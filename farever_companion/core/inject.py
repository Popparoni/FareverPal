"""Code-detour injector for the read-only-EFFECT player hook.

This is the ONLY part of the companion that writes to the game, and it does so
narrowly: it allocates a trampoline page inside the game, steals N bytes at one
unique code site, and redirects them to the trampoline (which copies a register
into our slot and jumps back). It touches no game data or save files; the patch
is restored on `disable()`. Ported from the proven tools/farever_qa injector.

It opens its OWN write handle by PID (PROCESS_VM_WRITE|OPERATION) so the main
`Proc` (the Rust reader) stays strictly read-only.
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from typing import Callable

from .proc import ProcError

# --- Win32 -----------------------------------------------------------------
MEM_COMMIT, MEM_RESERVE, MEM_RELEASE, MEM_FREE = 0x1000, 0x2000, 0x8000, 0x10000
PAGE_EXECUTE_READWRITE = 0x40
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400

_NEAR_WINDOW = 0x70000000   # rel32 reaches ±2GB; keep margin
_ALLOC_GRAN = 0x10000
_USER_MAX = 0x7FFFFFFEFFFF


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", ctypes.c_ulong),
        ("__alignment1", ctypes.c_ulong),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("__alignment2", ctypes.c_ulong),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_k32.VirtualAllocEx.restype = ctypes.c_void_p
_k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
_k32.VirtualFreeEx.restype = wintypes.BOOL
_k32.VirtualProtectEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k32.VirtualProtectEx.restype = wintypes.BOOL
_k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
_k32.VirtualQueryEx.restype = ctypes.c_size_t
_k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.WriteProcessMemory.restype = wintypes.BOOL
_k32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
_k32.FlushInstructionCache.restype = wintypes.BOOL


# --- trampoline helpers ----------------------------------------------------
def jmp_rel32(src_addr: int, dst_addr: int) -> bytes:
    rel = dst_addr - (src_addr + 5)
    if not (-0x80000000 <= rel <= 0x7FFFFFFF):
        raise ProcError(f"jmp target {dst_addr:#x} out of rel32 range from {src_addr:#x}")
    return b"\xE9" + struct.pack("<i", rel)


def detour_patch(hook_addr: int, tramp_addr: int, stolen_len: int) -> bytes:
    if stolen_len < 5:
        raise ProcError(f"stolen_len {stolen_len} < 5; cannot fit a rel32 JMP")
    return jmp_rel32(hook_addr, tramp_addr) + b"\x90" * (stolen_len - 5)


class Injector:
    """A write/alloc handle to the target, opened by PID. Separate from the
    read-only Rust reader so reads never share a writable handle."""

    def __init__(self, pid: int):
        self.pid = pid
        access = (PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ
                  | PROCESS_QUERY_INFORMATION)
        h = _k32.OpenProcess(access, False, pid)
        if not h:
            raise ProcError(
                f"OpenProcess(write) failed for pid {pid} "
                f"(err {ctypes.get_last_error()}); run elevated if the game is elevated")
        self.handle = h

    def write_bytes(self, addr: int, data: bytes) -> None:
        put = ctypes.c_size_t(0)
        buf = (ctypes.c_char * len(data)).from_buffer_copy(data)
        ok = _k32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), buf,
                                     len(data), ctypes.byref(put))
        if not ok or put.value != len(data):
            raise ProcError(f"write {len(data)}B @ {addr:#x} failed (err {ctypes.get_last_error()})")

    def write_code(self, addr: int, data: bytes) -> None:
        """Write into (possibly read-only) executable memory: flip to RWX,
        write, restore original protection, flush the icache."""
        old = wintypes.DWORD(0)
        if not _k32.VirtualProtectEx(self.handle, ctypes.c_void_p(addr), len(data),
                                     PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise ProcError(f"VirtualProtectEx(RWX) @ {addr:#x} failed (err {ctypes.get_last_error()})")
        try:
            self.write_bytes(addr, data)
        finally:
            restore = wintypes.DWORD(0)
            _k32.VirtualProtectEx(self.handle, ctypes.c_void_p(addr), len(data),
                                  old.value, ctypes.byref(restore))
        _k32.FlushInstructionCache(self.handle, ctypes.c_void_p(addr), len(data))

    def _free_regions(self, lo: int, hi: int):
        addr = lo
        mbi = MEMORY_BASIC_INFORMATION()
        size = ctypes.sizeof(mbi)
        while addr < hi:
            if not _k32.VirtualQueryEx(self.handle, ctypes.c_void_p(addr),
                                       ctypes.byref(mbi), size):
                break
            yield mbi.BaseAddress, mbi.RegionSize, mbi.State
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt

    def alloc_near(self, near: int, size: int = 0x1000) -> int:
        """Allocate an RWX page within ±2GB of `near` so a 5-byte rel32 jmp can
        reach it. Probes just-above the target first, then just-below."""
        lo = max(near - _NEAR_WINDOW, _ALLOC_GRAN)
        hi = min(near + _NEAR_WINDOW, _USER_MAX)

        def _window(win_lo: int, win_hi: int) -> int | None:
            for base0, rsize, state in self._free_regions(win_lo, win_hi):
                if state != MEM_FREE:
                    continue
                base = (max(base0, win_lo) + _ALLOC_GRAN - 1) & ~(_ALLOC_GRAN - 1)
                region_end = base0 + rsize
                while base + size <= region_end and base < win_hi:
                    p = _k32.VirtualAllocEx(self.handle, ctypes.c_void_p(base), size,
                                            MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
                    if p:
                        return int(p)
                    base += _ALLOC_GRAN
            return None

        addr = _window(near, hi) or _window(lo, near)
        if addr is None:
            raise ProcError(
                f"no free page within ±{_NEAR_WINDOW:#x} of {near:#x} for a trampoline")
        return addr

    def free(self, addr: int) -> None:
        if not _k32.VirtualFreeEx(self.handle, ctypes.c_void_p(addr), 0, MEM_RELEASE):
            raise ProcError(f"VirtualFreeEx @ {addr:#x} failed (err {ctypes.get_last_error()})")

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


TrampolineBuilder = Callable[[int, int, bytes], bytes]


class Hook:
    """One installed detour. `build(tramp_addr, hook_addr, original) -> bytes`
    returns the full trampoline body. Reads the original bytes via `proc`
    (read-only); all writes go through its own `Injector`."""

    def __init__(self, proc, hook_addr: int, stolen_len: int, build: TrampolineBuilder):
        self.proc = proc
        self.hook_addr = hook_addr
        self.stolen_len = stolen_len
        self.build = build
        self.injector = Injector(proc.pid)
        self.original: bytes | None = None
        self.tramp_addr: int | None = None
        self.active = False

    def enable(self) -> None:
        if self.active:
            return
        self.original = self.proc.read(self.hook_addr, self.stolen_len)
        self.tramp_addr = self.injector.alloc_near(self.hook_addr, 0x1000)
        try:
            tramp = self.build(self.tramp_addr, self.hook_addr, self.original)
            self.injector.write_bytes(self.tramp_addr, tramp)
            self.injector.write_code(
                self.hook_addr, detour_patch(self.hook_addr, self.tramp_addr, self.stolen_len))
        except Exception:
            try:
                self.injector.free(self.tramp_addr)
            except ProcError:
                pass
            self.tramp_addr = None
            raise
        self.active = True

    def disable(self) -> None:
        if not self.active:
            return
        try:
            self.injector.write_code(self.hook_addr, self.original)
            if self.tramp_addr is not None:
                self.injector.free(self.tramp_addr)
        finally:
            self.tramp_addr = None
            self.active = False
            self.injector.close()
