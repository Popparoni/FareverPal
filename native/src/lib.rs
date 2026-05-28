//! Read-only Windows process memory reader for the Farever Companion.
//!
//! Exposes a `Reader` to Python (via PyO3) that attaches by process name and
//! offers: single reads, BATCHED reads (`read_many` — one FFI call for a whole
//! scene snapshot), region enumeration, a fast value/pattern scan (`find_bytes`
//! — the primitive used to locate the player by pure read), and module base
//! lookup. Write/alloc/protect exist but require the explicit RW attach and are
//! unused by the read-only app (kept only for the optional hook fallback).
//!
//! All scanning/large reads release the GIL so the UI stays responsive.

use std::ffi::c_void;
use std::mem::{size_of, zeroed};

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};

use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::Debug::{ReadProcessMemory, WriteProcessMemory};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Module32FirstW, Module32NextW, Process32FirstW, Process32NextW,
    MODULEENTRY32W, PROCESSENTRY32W, TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32, TH32CS_SNAPPROCESS,
};
use windows_sys::Win32::System::Memory::{
    VirtualAllocEx, VirtualProtectEx, VirtualQueryEx, MEMORY_BASIC_INFORMATION, MEM_COMMIT,
    MEM_RESERVE, PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY, PAGE_GUARD,
    PAGE_NOACCESS, PAGE_READONLY, PAGE_READWRITE, PAGE_WRITECOPY,
};
use windows_sys::Win32::System::Threading::{
    OpenProcess, PROCESS_QUERY_INFORMATION, PROCESS_VM_OPERATION, PROCESS_VM_READ,
    PROCESS_VM_WRITE,
};

type Handle = *mut c_void;

const CHUNK: usize = 4 * 1024 * 1024; // 4 MiB scan window

fn readable(protect: u32) -> bool {
    if protect & PAGE_GUARD != 0 || protect & PAGE_NOACCESS != 0 {
        return false;
    }
    protect
        & (PAGE_READONLY
            | PAGE_READWRITE
            | PAGE_WRITECOPY
            | PAGE_EXECUTE_READ
            | PAGE_EXECUTE_READWRITE
            | PAGE_EXECUTE_WRITECOPY)
        != 0
}

fn writable(protect: u32) -> bool {
    protect & (PAGE_READWRITE | PAGE_EXECUTE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_WRITECOPY)
        != 0
}

/// Lowercase comparison of a wide (UTF-16) C string against an ASCII name.
fn wide_eq_ascii_ci(wide: &[u16], name: &str) -> bool {
    let end = wide.iter().position(|&c| c == 0).unwrap_or(wide.len());
    let s = String::from_utf16_lossy(&wide[..end]);
    s.eq_ignore_ascii_case(name)
}

fn find_pid(name: &str) -> Option<u32> {
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return None;
        }
        let mut pe: PROCESSENTRY32W = zeroed();
        pe.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snap, &mut pe);
        let mut found = None;
        while ok != 0 {
            if wide_eq_ascii_ci(&pe.szExeFile, name) {
                found = Some(pe.th32ProcessID);
                break;
            }
            ok = Process32NextW(snap, &mut pe);
        }
        CloseHandle(snap);
        found
    }
}

fn module_base_for(pid: u32, module: &str) -> Option<u64> {
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
        if snap == INVALID_HANDLE_VALUE {
            return None;
        }
        let mut me: MODULEENTRY32W = zeroed();
        me.dwSize = size_of::<MODULEENTRY32W>() as u32;
        let mut ok = Module32FirstW(snap, &mut me);
        let mut base = None;
        while ok != 0 {
            if wide_eq_ascii_ci(&me.szModule, module) {
                base = Some(me.modBaseAddr as u64);
                break;
            }
            ok = Module32NextW(snap, &mut me);
        }
        CloseHandle(snap);
        base
    }
}

#[pyclass]
struct Reader {
    handle: isize, // HANDLE stored as isize so the pyclass stays Send
    #[pyo3(get)]
    pid: u32,
    #[pyo3(get)]
    name: String,
    #[pyo3(get)]
    writable: bool,
}

impl Reader {
    fn h(&self) -> Handle {
        self.handle as Handle
    }

    /// Raw read into a fresh Vec; None if the read fails or is short.
    fn raw_read(&self, addr: u64, len: usize) -> Option<Vec<u8>> {
        if len == 0 {
            return Some(Vec::new());
        }
        let mut buf = vec![0u8; len];
        let mut got: usize = 0;
        let ok = unsafe {
            ReadProcessMemory(
                self.h(),
                addr as *const c_void,
                buf.as_mut_ptr() as *mut c_void,
                len,
                &mut got,
            )
        };
        if ok != 0 && got == len {
            Some(buf)
        } else {
            None
        }
    }
}

#[pymethods]
impl Reader {
    #[staticmethod]
    #[pyo3(signature = (name, write=false))]
    fn attach(name: &str, write: bool) -> PyResult<Reader> {
        let pid = find_pid(name)
            .ok_or_else(|| PyOSError::new_err(format!("process '{name}' not found")))?;
        let mut access = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION;
        if write {
            access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION;
        }
        let handle = unsafe { OpenProcess(access, 0, pid) };
        if handle.is_null() {
            return Err(PyOSError::new_err(format!(
                "OpenProcess failed for pid {pid} (try running elevated if the game is elevated)"
            )));
        }
        Ok(Reader {
            handle: handle as isize,
            pid,
            name: name.to_string(),
            writable: write,
        })
    }

    /// Base address of a loaded module (default: the process exe).
    #[pyo3(signature = (module=None))]
    fn module_base(&self, module: Option<&str>) -> PyResult<u64> {
        let m = module.unwrap_or(&self.name);
        module_base_for(self.pid, m)
            .ok_or_else(|| PyOSError::new_err(format!("module '{m}' not found")))
    }

    /// Read `len` bytes at `addr`. Raises OSError on failure.
    fn read(&self, py: Python<'_>, addr: u64, len: usize) -> PyResult<Py<PyBytes>> {
        match self.raw_read(addr, len) {
            Some(b) => Ok(PyBytes::new(py, &b).unbind()),
            None => Err(PyOSError::new_err(format!("read {len}B @ {addr:#x} failed"))),
        }
    }

    /// Read `len` bytes at `addr`, or None if unreadable (no exception).
    fn try_read(&self, py: Python<'_>, addr: u64, len: usize) -> Option<Py<PyBytes>> {
        self.raw_read(addr, len).map(|b| PyBytes::new(py, &b).unbind())
    }

    fn read_u64(&self, addr: u64) -> PyResult<u64> {
        self.raw_read(addr, 8)
            .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
            .ok_or_else(|| PyOSError::new_err(format!("read_u64 @ {addr:#x} failed")))
    }

    fn read_i32(&self, addr: u64) -> PyResult<i32> {
        self.raw_read(addr, 4)
            .map(|b| i32::from_le_bytes(b.try_into().unwrap()))
            .ok_or_else(|| PyOSError::new_err(format!("read_i32 @ {addr:#x} failed")))
    }

    fn read_f64(&self, addr: u64) -> PyResult<f64> {
        self.raw_read(addr, 8)
            .map(|b| f64::from_le_bytes(b.try_into().unwrap()))
            .ok_or_else(|| PyOSError::new_err(format!("read_f64 @ {addr:#x} failed")))
    }

    /// Batched read: one FFI call for many addresses of equal `size`.
    /// Returns a list where each element is `bytes` (read OK) or `None`
    /// (unreadable). Releases the GIL across the whole batch.
    fn read_many(
        &self,
        py: Python<'_>,
        addrs: Vec<u64>,
        size: usize,
    ) -> PyResult<Py<PyList>> {
        let results: Vec<Option<Vec<u8>>> =
            py.allow_threads(|| addrs.iter().map(|&a| self.raw_read(a, size)).collect());
        let list = PyList::empty(py);
        for r in results {
            match r {
                Some(b) => list.append(PyBytes::new(py, &b))?,
                None => list.append(py.None())?,
            }
        }
        Ok(list.unbind())
    }

    /// Batched u64 pointer read: list of int (or None). Convenience over
    /// read_many for pointer-chasing.
    fn read_many_u64(&self, py: Python<'_>, addrs: Vec<u64>) -> PyResult<Py<PyList>> {
        let results: Vec<Option<u64>> = py.allow_threads(|| {
            addrs
                .iter()
                .map(|&a| {
                    self.raw_read(a, 8)
                        .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
                })
                .collect()
        });
        let list = PyList::empty(py);
        for r in results {
            match r {
                Some(v) => list.append(v)?,
                None => list.append(py.None())?,
            }
        }
        Ok(list.unbind())
    }

    /// Enumerate committed memory regions as (base, size, protect, rw_flag).
    fn regions(&self, py: Python<'_>) -> Vec<(u64, u64, u32, bool)> {
        py.allow_threads(|| {
            let mut out = Vec::new();
            let mut addr: usize = 0;
            loop {
                let mut mbi: MEMORY_BASIC_INFORMATION = unsafe { zeroed() };
                let r = unsafe {
                    VirtualQueryEx(
                        self.h(),
                        addr as *const c_void,
                        &mut mbi,
                        size_of::<MEMORY_BASIC_INFORMATION>(),
                    )
                };
                if r == 0 {
                    break;
                }
                let base = mbi.BaseAddress as usize;
                let rsize = mbi.RegionSize;
                if mbi.State == MEM_COMMIT && readable(mbi.Protect) {
                    out.push((base as u64, rsize as u64, mbi.Protect, writable(mbi.Protect)));
                }
                match base.checked_add(rsize) {
                    Some(n) if n > addr => addr = n,
                    _ => break,
                }
            }
            out
        })
    }

    /// Scan committed memory for `needle`, returning matching absolute
    /// addresses (capped at `max_hits`). `align` (e.g. 8) restricts to aligned
    /// addresses; `rw_only` skips code/read-only pages (heap-object scan).
    /// Runs entirely in Rust with the GIL released.
    #[pyo3(signature = (needle, align=1, rw_only=false, max_hits=4096))]
    fn find_bytes(
        &self,
        py: Python<'_>,
        needle: Vec<u8>,
        align: usize,
        rw_only: bool,
        max_hits: usize,
    ) -> PyResult<Vec<u64>> {
        if needle.is_empty() {
            return Err(PyValueError::new_err("needle must be non-empty"));
        }
        let align = align.max(1);
        Ok(py.allow_threads(|| {
            let mut hits = Vec::new();
            let nlen = needle.len();
            let mut addr: usize = 0;
            'regions: loop {
                let mut mbi: MEMORY_BASIC_INFORMATION = unsafe { zeroed() };
                let r = unsafe {
                    VirtualQueryEx(
                        self.h(),
                        addr as *const c_void,
                        &mut mbi,
                        size_of::<MEMORY_BASIC_INFORMATION>(),
                    )
                };
                if r == 0 {
                    break;
                }
                let base = mbi.BaseAddress as usize;
                let rsize = mbi.RegionSize;
                let scan_this = mbi.State == MEM_COMMIT
                    && readable(mbi.Protect)
                    && (!rw_only || writable(mbi.Protect));
                if scan_this {
                    let mut off = 0usize;
                    let mut buf = vec![0u8; CHUNK + nlen];
                    while off < rsize {
                        let want = (CHUNK + nlen - 1).min(rsize - off);
                        let mut got = 0usize;
                        let ok = unsafe {
                            ReadProcessMemory(
                                self.h(),
                                (base + off) as *const c_void,
                                buf.as_mut_ptr() as *mut c_void,
                                want,
                                &mut got,
                            )
                        };
                        if ok != 0 && got >= nlen {
                            let buf_addr = (base + off) as u64;
                            // first index whose absolute address is aligned
                            let rem = (buf_addr % align as u64) as usize;
                            let mut i = if rem == 0 { 0 } else { align - rem };
                            while i + nlen <= got {
                                if &buf[i..i + nlen] == needle.as_slice() {
                                    hits.push(buf_addr + i as u64);
                                    if hits.len() >= max_hits {
                                        break 'regions;
                                    }
                                }
                                i += align;
                            }
                        }
                        off += CHUNK;
                    }
                }
                match base.checked_add(rsize) {
                    Some(n) if n > addr => addr = n,
                    _ => break,
                }
            }
            hits
        }))
    }

    // --- write side (RW attach only; unused by the read-only app) ---------
    fn write(&self, addr: u64, data: Vec<u8>) -> PyResult<()> {
        if !self.writable {
            return Err(PyOSError::new_err("reader attached read-only; use attach(write=True)"));
        }
        let mut put = 0usize;
        let ok = unsafe {
            WriteProcessMemory(
                self.h(),
                addr as *const c_void,
                data.as_ptr() as *const c_void,
                data.len(),
                &mut put,
            )
        };
        if ok != 0 && put == data.len() {
            Ok(())
        } else {
            Err(PyOSError::new_err(format!("write @ {addr:#x} failed")))
        }
    }

    #[pyo3(signature = (size, near=0))]
    fn alloc(&self, size: usize, near: u64) -> PyResult<u64> {
        if !self.writable {
            return Err(PyOSError::new_err("reader attached read-only"));
        }
        let _ = near; // near-allocation search is the caller's job if needed
        let p = unsafe {
            VirtualAllocEx(
                self.h(),
                std::ptr::null(),
                size,
                MEM_COMMIT | MEM_RESERVE,
                PAGE_EXECUTE_READWRITE,
            )
        };
        if p.is_null() {
            Err(PyOSError::new_err("VirtualAllocEx failed"))
        } else {
            Ok(p as u64)
        }
    }

    fn protect(&self, addr: u64, size: usize, new_protect: u32) -> PyResult<u32> {
        if !self.writable {
            return Err(PyOSError::new_err("reader attached read-only"));
        }
        let mut old: u32 = 0;
        let ok = unsafe {
            VirtualProtectEx(self.h(), addr as *const c_void, size, new_protect, &mut old)
        };
        if ok != 0 {
            Ok(old)
        } else {
            Err(PyOSError::new_err("VirtualProtectEx failed"))
        }
    }

    fn close(&mut self) {
        if self.handle != 0 {
            unsafe { CloseHandle(self.h()) };
            self.handle = 0;
        }
    }
}

impl Drop for Reader {
    fn drop(&mut self) {
        self.close();
    }
}

#[pyfunction]
fn pid_of(name: &str) -> Option<u32> {
    find_pid(name)
}

#[pymodule]
fn farever_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Reader>()?;
    m.add_function(wrap_pyfunction!(pid_of, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
