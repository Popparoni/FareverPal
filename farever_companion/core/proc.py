"""Process access — uniform interface over the Rust reader (preferred) or
pymem (fallback).

The Rust `farever_native.Reader` is the primary backend: it offers batched reads
(`read_many`) and a fast `find_bytes` scan that the pure-read player locator
needs. If the extension isn't built, we fall back to pymem for basic reads; the
scan-dependent features (player locate) then require building the extension.

All methods are read-only by default. `attach(write=True)` opens write access
only for the (disabled) hook fallback; the app never uses it.
"""
from __future__ import annotations

import struct

try:
    import farever_native as _native
except ImportError:  # pragma: no cover - extension optional
    _native = None

try:
    import pymem as _pymem
except ImportError:  # pragma: no cover
    _pymem = None

PROCESS_NAME = "Farever.exe"


class ProcError(RuntimeError):
    pass


def backend_name() -> str:
    if _native is not None:
        return "native"
    if _pymem is not None:
        return "pymem"
    return "none"


def find_pid(name: str = PROCESS_NAME) -> int | None:
    """First PID whose image name == `name` (case-insensitive), or None.

    Read-only: walks the Win32 Toolhelp process snapshot via ctypes and opens no
    process handle, so it's cheap and safe to poll every couple of seconds. No
    new dependency (the app is Windows-only). Returns None on any failure or if
    the process isn't running.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - non-Windows / no ctypes
        return None

    TH32CS_SNAPPROCESS = 0x2

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    try:
        k32 = ctypes.windll.kernel32
    except Exception:  # pragma: no cover - non-Windows
        return None
    INVALID = ctypes.c_void_p(-1).value
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, None, -1, INVALID):
        return None
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        target = name.lower()
        while ok:
            if e.szExeFile.lower() == target:
                return int(e.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return None
    finally:
        k32.CloseHandle(snap)


class Proc:
    """Read-only handle to the game process."""

    def __init__(self, impl, kind: str, pid: int, name: str):
        self._impl = impl
        self.kind = kind
        self.pid = pid
        self.name = name

    # --- lifecycle -------------------------------------------------------
    @classmethod
    def attach(cls, name: str = PROCESS_NAME, write: bool = False) -> "Proc":
        if _native is not None:
            try:
                r = _native.Reader.attach(name, write)
            except OSError as e:
                raise ProcError(str(e)) from e
            return cls(r, "native", r.pid, name)
        if _pymem is not None:
            try:
                pm = _pymem.Pymem(name)
            except Exception as e:  # pymem raises its own types
                raise ProcError(f"pymem attach failed: {e}") from e
            return cls(pm, "pymem", pm.process_id, name)
        raise ProcError("no memory backend available (build farever_native or install pymem)")

    def close(self) -> None:
        try:
            if self.kind == "native":
                self._impl.close()
            elif self.kind == "pymem":
                self._impl.close_process()
        except Exception:
            pass

    @property
    def has_scan(self) -> bool:
        return self.kind == "native"

    # --- reads -----------------------------------------------------------
    def read(self, addr: int, n: int) -> bytes:
        try:
            if self.kind == "native":
                return self._impl.read(addr, n)
            return self._impl.read_bytes(addr, n)
        except Exception as e:
            raise ProcError(f"read {n}B @ {addr:#x}: {e}") from e

    def try_read(self, addr: int, n: int) -> bytes | None:
        if self.kind == "native":
            return self._impl.try_read(addr, n)
        try:
            return self._impl.read_bytes(addr, n)
        except Exception:
            return None

    def u64(self, addr: int) -> int:
        if self.kind == "native":
            return self._impl.read_u64(addr)
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def i32(self, addr: int) -> int:
        if self.kind == "native":
            return self._impl.read_i32(addr)
        return struct.unpack("<i", self.read(addr, 4))[0]

    def f64(self, addr: int) -> float:
        if self.kind == "native":
            return self._impl.read_f64(addr)
        return struct.unpack("<d", self.read(addr, 8))[0]

    def read_many(self, addrs: list[int], size: int) -> list[bytes | None]:
        """One batched read of many equal-size blocks (native), or a loop."""
        if self.kind == "native":
            return list(self._impl.read_many(addrs, size))
        out: list[bytes | None] = []
        for a in addrs:
            out.append(self.try_read(a, size))
        return out

    def read_many_u64(self, addrs: list[int]) -> list[int | None]:
        if self.kind == "native":
            return list(self._impl.read_many_u64(addrs))
        out: list[int | None] = []
        for a in addrs:
            b = self.try_read(a, 8)
            out.append(struct.unpack("<Q", b)[0] if b else None)
        return out

    # --- scan / modules --------------------------------------------------
    def find_bytes(self, needle: bytes, align: int = 1, rw_only: bool = False,
                   max_hits: int = 4096) -> list[int]:
        if self.kind != "native":
            raise ProcError("find_bytes needs the farever_native extension (memory scan)")
        return list(self._impl.find_bytes(needle, align, rw_only, max_hits))

    def find_qword(self, value: int, rw_only: bool = True, max_hits: int = 4096) -> list[int]:
        """Aligned scan for an 8-byte value (pointer / type tag)."""
        return self.find_bytes(struct.pack("<Q", value), 8, rw_only, max_hits)

    def find_bytes_in(self, needle: bytes, ranges: list[tuple[int, int]],
                      align: int = 1, max_hits: int = 4096) -> list[int]:
        """Scan ONLY `ranges` (each `(base, len)`) for `needle`. The fast path for
        re-enumerating a clustered object type: the caller hands in the small set
        of GC size-class page ranges instead of paying a full ~23 GB heap walk."""
        if self.kind != "native":
            raise ProcError("find_bytes_in needs the farever_native extension (memory scan)")
        return list(self._impl.find_bytes_in(needle, ranges, align, max_hits))

    def find_qword_in(self, value: int, ranges: list[tuple[int, int]],
                      max_hits: int = 4096) -> list[int]:
        """Aligned 8-byte scan for `value`, restricted to `ranges`."""
        return self.find_bytes_in(struct.pack("<Q", value), ranges, 8, max_hits)

    def regions(self) -> list[tuple[int, int, int, bool]]:
        """Committed readable regions as (base, size, protect, writable). Native
        only (used by the AOB scanner)."""
        if self.kind != "native":
            raise ProcError("regions needs the farever_native extension")
        return list(self._impl.regions())

    def module_base(self, module: str | None = None) -> int:
        if self.kind == "native":
            return self._impl.module_base(module)
        # pymem
        if module is None or module.lower() == self.name.lower():
            return self._impl.base_address
        for m in self._impl.list_modules():
            if m.name.lower() == module.lower():
                return m.lpBaseOfDll
        raise ProcError(f"module '{module}' not found")
