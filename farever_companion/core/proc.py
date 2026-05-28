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
