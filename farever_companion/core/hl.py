"""HashLink runtime reflection over the live process (read-only).

Every Haxe→HashLink heap object carries its runtime type, so we read class
names, super-chains, strings and arrays straight from memory. Object model
(HL 64-bit, validated 2026-05; array layout = post-2026-05-21 patch):

    instance[+0]            -> hl_type*
    hl_type[+0]             : kind (i32)   HOBJ=11, HSTRUCT=21
    hl_type[+8]             -> hl_type_obj*
    hl_type_obj[+0]         : nfields (i32)
    hl_type_obj[+0x10]      -> name (uchar*, UTF-16LE, null-terminated)
    hl_type_obj[+0x18]      -> super (hl_type*)
    hl_type_obj[+0x20]      -> fields (hl_obj_field[]; each 0x18: name*, type*, hash)
    String[+0x08]           -> bytes (UTF-16LE);  String[+0x10] : length (i32)
    ArrayObj[+0x08]         : length (i32);  ArrayObj[+0x10] -> NativeArray
    NativeArray[+0x10]      : size (i32);  data @ +0x18 (8B object pointers)

Type pointers are stable for the process life, so class-name lookups cache by
type pointer; string reads cache by string-object pointer.
"""
from __future__ import annotations

import struct

from .proc import Proc, ProcError
from .constants import (
    HOBJ as _HOBJ, HSTRUCT as _HSTRUCT, USER_MIN as _USER_MIN,
    USER_MAX as _USER_MAX, TO_NAME, TO_SUPER, TO_FIELDS, FIELD_STRIDE,
    NA_SIZE_OFF, NA_DATA_OFF,
)


def is_ptr(v: int | None) -> bool:
    return v is not None and _USER_MIN < v < _USER_MAX


def utf16z(s: str) -> bytes:
    """A UTF-16LE, null-terminated class/name needle for a memory scan. Shared
    by the locators (appsingleton.py, player.py)."""
    return s.encode("utf-16-le") + b"\x00\x00"


class Hl:
    def __init__(self, proc: Proc):
        self.proc = proc
        self._name_cache: dict[int, str | None] = {}
        self._str_cache: dict[int, str | None] = {}
        self._anc_cache: dict[int, frozenset[str]] = {}

    # --- primitives ------------------------------------------------------
    def ptr(self, addr: int) -> int | None:
        v = self.proc.try_read(addr, 8)
        if v is None:
            return None
        p = struct.unpack("<Q", v)[0]
        return p if is_ptr(p) else None

    # The native backend raises OSError on a bad read; normalise to ProcError
    # so the many `except ProcError` guards below (and in callers) catch it.
    def u64(self, addr: int) -> int:
        try:
            return self.proc.u64(addr)
        except OSError as e:
            raise ProcError(str(e)) from e

    def i32(self, addr: int) -> int:
        try:
            return self.proc.i32(addr)
        except OSError as e:
            raise ProcError(str(e)) from e

    def f64(self, addr: int) -> float:
        try:
            return self.proc.f64(addr)
        except OSError as e:
            raise ProcError(str(e)) from e

    # --- type / class ----------------------------------------------------
    def _read_utf16(self, addr: int, max_chars: int = 256) -> str | None:
        raw = self.proc.try_read(addr, max_chars * 2)
        if raw is None:
            return None
        end = raw.find(b"\x00\x00")
        if end != -1 and end % 2 == 1:
            end += 1
        if end != -1:
            raw = raw[:end]
        return raw.decode("utf-16-le", errors="replace")

    def type_name(self, type_ptr: int) -> str | None:
        if type_ptr in self._name_cache:
            return self._name_cache[type_ptr]
        name = self._resolve_type_name(type_ptr)
        self._name_cache[type_ptr] = name
        return name

    def _resolve_type_name(self, type_ptr: int) -> str | None:
        if not is_ptr(type_ptr):
            return None
        try:
            if self.i32(type_ptr) not in (_HOBJ, _HSTRUCT):
                return None
            obj_ptr = self.u64(type_ptr + 8)
            if not is_ptr(obj_ptr):
                return None
            name_ptr = self.u64(obj_ptr + TO_NAME)
            if not is_ptr(name_ptr):
                return None
            return self._read_utf16(name_ptr)
        except ProcError:
            return None

    def class_of(self, instance: int) -> str | None:
        tp = self.ptr(instance)
        return self.type_name(tp) if tp is not None else None

    def super_chain(self, instance: int, limit: int = 16) -> list[str]:
        names: list[str] = []
        tp = self.ptr(instance)
        seen = set()
        while tp and tp not in seen and len(names) < limit:
            seen.add(tp)
            try:
                if self.i32(tp) not in (_HOBJ, _HSTRUCT):
                    break
                obj_ptr = self.u64(tp + 8)
                names.append(self._read_utf16(self.u64(obj_ptr + TO_NAME)) or "?")
                tp = self.u64(obj_ptr + TO_SUPER)
            except ProcError:
                break
        return names

    def is_a(self, instance: int, ancestor: str) -> bool:
        """True if `instance`'s class is, or descends from, `ancestor`.
        This is the fix for the old boss-HP bug (exact-class match missed
        Foe subclasses)."""
        return ancestor in self.super_chain(instance)

    def ancestors(self, type_ptr: int | None) -> frozenset[str]:
        """Cached set of class names in a TYPE's super-chain (incl. itself).
        Keyed by type pointer, so classifying many instances of the same type
        per frame costs one walk total."""
        if not is_ptr(type_ptr):
            return frozenset()
        cached = self._anc_cache.get(type_ptr)
        if cached is not None:
            return cached
        names: list[str] = []
        tp = type_ptr
        seen = set()
        while tp and tp not in seen and len(names) < 16:
            seen.add(tp)
            try:
                if self.i32(tp) not in (_HOBJ, _HSTRUCT):
                    break
                obj_ptr = self.u64(tp + 8)
                names.append(self._read_utf16(self.u64(obj_ptr + TO_NAME)) or "?")
                tp = self.u64(obj_ptr + TO_SUPER)
            except ProcError:
                break
        result = frozenset(names)
        self._anc_cache[type_ptr] = result
        return result

    def type_is_a(self, type_ptr: int | None, ancestor: str) -> bool:
        return ancestor in self.ancestors(type_ptr)

    def field_names(self, type_ptr: int) -> list[str]:
        """Declared field names of an object type (own fields only), in order.
        Reliable from hl_obj_field; byte offsets need live calibration (the
        runtime object's fields_indexes), which is why the readers in core/ use
        named, live-validated byte offsets rather than deriving them here."""
        if not is_ptr(type_ptr):
            return []
        try:
            if self.i32(type_ptr) not in (_HOBJ, _HSTRUCT):
                return []
            obj_ptr = self.u64(type_ptr + 8)
            n = self.i32(obj_ptr)
            fields = self.u64(obj_ptr + TO_FIELDS)
        except ProcError:
            return []
        if not is_ptr(fields) or not (0 <= n < 4096):
            return []
        out = []
        for i in range(n):
            try:
                name_ptr = self.u64(fields + i * FIELD_STRIDE)
                out.append(self._read_utf16(name_ptr) or "?")
            except ProcError:
                break
        return out

    # --- strings ---------------------------------------------------------
    def hl_string(self, strobj: int | None) -> str | None:
        if not is_ptr(strobj):
            return None
        if strobj in self._str_cache:
            return self._str_cache[strobj]
        val = self._read_string(strobj)
        self._str_cache[strobj] = val
        return val

    def _read_string(self, strobj: int) -> str | None:
        try:
            bptr = self.u64(strobj + 8)
            length = self.i32(strobj + 0x10)
        except ProcError:
            return None
        if not is_ptr(bptr) or not (0 <= length < 4096):
            return None
        raw = self.proc.try_read(bptr, length * 2)
        if raw is None:
            return None
        return raw.decode("utf-16-le", errors="replace")

    # --- arrays ----------------------------------------------------------
    def array(self, arrobj: int | None, max_elems: int = 1024) -> list[int]:
        if not is_ptr(arrobj):
            return []
        try:
            length = self.i32(arrobj + 8)
            backing = self.u64(arrobj + 0x10)
        except ProcError:
            return []
        if not is_ptr(backing) or not (0 <= length < 1_000_000):
            return []
        try:
            bsize = self.i32(backing + NA_SIZE_OFF)
        except ProcError:
            return []
        n = max(0, min(length, bsize, max_elems))
        raw = self.proc.try_read(backing + NA_DATA_OFF, n * 8)
        if raw is None:
            return []
        return [struct.unpack_from("<Q", raw, i * 8)[0] for i in range(n)]
