"""GameApp-singleton anchor — (nearly) scan-free, session-stable player locate.

Instead of scanning the multi-GB heap for `ent.Hero` instances every zone change
(slow, and needs disambiguating the local player from network proxies), we
anchor on the **GameApp singleton** and pointer-chase to the local Hero. Once the
anchor is resolved it is cached and stays valid for the whole session — `hero()`
just re-reads a field, following character/zone changes for free.

The chain, validated live (2026-06-02, see _diag_globalvalue.py):

    GameApp hl_type --(super, +0x18)--> App hl_type
    App hl_type_obj --(global_value, +0x38)--> &globals_data slot for $App
        *slot --> $App static object
            .inst (a GameApp)            == App.inst, the singleton
                GameApp.hero  : ent.Hero      <- THE local player
                GameApp.me    : st.Player
                GameApp.layer : st.GameLayer

Everything past the GameApp *type* is a field read — no heap scan. The only scan
is finding the GameApp type once: a single full pass for the UTF-16 class name,
then a tiny region-bounded pass for the type_obj that references it. Read-only
throughout; the app never writes to the game.

In-object field offsets (inst, hero, me, layer) and the global_value offset are
validated against this build but re-resolved live **by class name** so they
survive patches; resolved values are cached and logged.
"""
from __future__ import annotations

import struct

from .hl import Hl, is_ptr, _HOBJ
from .proc import Proc, ProcError

# Anchor class names.
CLS_GAMEAPP = "GameApp"
CLS_APP = "App"
CLS_HERO = "ent.Hero"
CLS_PLAYER = "st.Player"
CLS_LAYER = "st.GameLayer"

# hl_type_obj field offsets (HL 64-bit; validated this build).
TO_NAME = 0x10
TO_SUPER = 0x18
TO_GLOBALVAL = 0x38

# How far into an object to look for the pointer fields we resolve by class.
_FIELD_SCAN_BYTES = 0x400


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


class GameAppLocator:
    def __init__(self, proc: Proc, hl: Hl | None = None):
        self.proc = proc
        self.hl = hl or Hl(proc)
        self._gameapp_obj: int | None = None   # GameApp hl_type_obj
        self._addr: int | None = None          # GameApp singleton instance
        self.off_inst: int | None = None       # $App static obj -> inst
        self.off_hero: int | None = None
        self.off_me: int | None = None
        self.off_layer: int | None = None

    # --- low-level safe reads --------------------------------------------
    def _u64(self, addr: int) -> int | None:
        b = self.proc.try_read(addr, 8)
        return struct.unpack("<Q", b)[0] if b else None

    # --- type_obj lookup by name (the only scan) -------------------------
    # The HL type metadata + globals_data live in small RW regions clustered low
    # in the address space (~0x2aa47… in this build) and are hot (in the working
    # set), whereas the multi-GB GC object heap sits higher and is largely paged
    # out. So we walk regions ASCENDING, scan only writable ones, and early-exit
    # the moment the chain validates — we never touch the cold heap. Big regions
    # are skipped during the name search (type-name pools are small).
    _MAX_NAME_REGION = 0x4000000   # 64 MB — metadata regions are far smaller

    def _committed_ranges_near(self, addr: int, span: int = 0x1000000
                               ) -> list[tuple[int, int]]:
        """Committed regions overlapping [addr-span, addr+span], each as its own
        (base, size) so a scan never straddles an unmapped gap."""
        lo, hi = addr - span, addr + span
        out = []
        for (base, size, _prot, _w) in self.proc.regions():
            if base + size <= lo or base >= hi:
                continue
            out.append((base, size))
        return out

    def _typeobj_candidates(self, name_addr: int) -> list[int]:
        """Every structure with a pointer to `name_addr` at +0x10 — candidate
        hl_type_objs. Several spots reference a type-name string, so this is just
        a candidate set; the real one is decided by running the chain."""
        out = []
        for ref in self.proc.find_qword_in(
                name_addr, self._committed_ranges_near(name_addr), max_hits=64):
            out.append(ref - TO_NAME)
        return out

    def _iter_gameapp_type_objs(self):
        """Yield candidate GameApp hl_type_objs by an ascending, early-ish walk
        over small writable regions (skips the cold multi-GB GC heap)."""
        if not self.proc.has_scan:
            raise ProcError("locate needs the farever_native scan backend")
        needle = _utf16z(CLS_GAMEAPP)
        regs = sorted(self.proc.regions(), key=lambda r: r[0])
        for (base, size, _prot, writable) in regs:
            if not writable or size > self._MAX_NAME_REGION:
                continue
            for name_addr in self.proc.find_bytes_in(needle, [(base, size)],
                                                     align=2, max_hits=8):
                if self.hl._read_utf16(name_addr) != CLS_GAMEAPP:
                    continue
                yield from self._typeobj_candidates(name_addr)

    def _typeobj_name(self, type_obj: int) -> str | None:
        nm = self._u64(type_obj + TO_NAME)
        return self.hl._read_utf16(nm) if nm else None

    # --- the no-scan chain -----------------------------------------------
    def _static_app_obj_from(self, gameapp_obj: int) -> int | None:
        """$App static object via GameApp.super(App).global_value, for a specific
        candidate GameApp type_obj. Returns None if the candidate isn't a real
        type_obj (the chain dereferences garbage and bails)."""
        app_type = self._u64(gameapp_obj + TO_SUPER)              # App hl_type
        if not is_ptr(app_type):
            return None
        if self.hl.type_name(app_type) != CLS_APP:               # super must be App
            return None
        app_obj = self._u64(app_type + 8)                        # App hl_type_obj
        if not is_ptr(app_obj):
            return None
        slot = self._u64(app_obj + TO_GLOBALVAL)                 # &globals_data[..]
        if not is_ptr(slot):
            return None
        static_obj = self._u64(slot)                             # $App static object
        return static_obj if is_ptr(static_obj) else None

    def _scan_for_class(self, base: int, wanted: dict[str, str]) -> dict[str, int]:
        """Map {class_name: attr} -> {attr: offset} by reading `base`'s pointer
        slots and matching the class each points at. Returns first match each."""
        out: dict[str, int] = {}
        raw = self.proc.try_read(base, _FIELD_SCAN_BYTES)
        if raw is None:
            return out
        for off in range(8, len(raw) - 8, 8):
            ptr = struct.unpack_from("<Q", raw, off)[0]
            if not is_ptr(ptr):
                continue
            cls = self.hl.class_of(ptr)
            attr = wanted.get(cls) if cls else None
            if attr and attr not in out:
                out[attr] = off
                if len(out) == len(wanted):
                    break
        return out

    def _try_chain(self, gameapp_obj: int) -> int | None:
        """Full chain for one candidate type_obj -> validated GameApp singleton
        (with hero/me/layer offsets resolved), or None."""
        static_obj = self._static_app_obj_from(gameapp_obj)
        if static_obj is None:
            return None
        inst_m = self._scan_for_class(static_obj, {CLS_GAMEAPP: "inst"})
        off_inst = inst_m.get("inst")
        if off_inst is None:
            return None
        singleton = self._u64(static_obj + off_inst)
        if not is_ptr(singleton) or self.hl.class_of(singleton) != CLS_GAMEAPP:
            return None
        fields = self._scan_for_class(singleton, {
            CLS_HERO: "hero", CLS_PLAYER: "me", CLS_LAYER: "layer"})
        if "hero" not in fields:
            return None
        hero = self._u64(singleton + fields["hero"])
        if not is_ptr(hero) or self.hl.class_of(hero) != CLS_HERO:
            return None
        # commit
        self._gameapp_obj = gameapp_obj
        self.off_inst = off_inst
        self.off_hero = fields.get("hero")
        self.off_me = fields.get("me")
        self.off_layer = fields.get("layer")
        return singleton

    # --- locate ----------------------------------------------------------
    def locate(self) -> int | None:
        # cached singleton stays valid for the session
        if self._addr is not None and self.hl.class_of(self._addr) == CLS_GAMEAPP:
            return self._addr
        self._addr = None
        # fast path: we already know the right type_obj from a previous locate
        if self._gameapp_obj is not None:
            singleton = self._try_chain(self._gameapp_obj)
            if singleton is not None:
                self._addr = singleton
                return singleton
            self._gameapp_obj = None
        # cold path: find the type_obj whose chain yields a real Hero
        for cand in self._iter_gameapp_type_objs():
            singleton = self._try_chain(cand)
            if singleton is not None:
                self._addr = singleton
                return singleton
        return None

    # --- reads (pure field follow, no scan) ------------------------------
    def hero(self) -> int | None:
        if self._addr is None or self.off_hero is None:
            if self.locate() is None:
                return None
        return self.hl.ptr(self._addr + self.off_hero)

    def me(self) -> int | None:
        if self._addr is None or self.off_me is None:
            return None
        return self.hl.ptr(self._addr + self.off_me)

    def layer(self) -> int | None:
        if self._addr is None or self.off_layer is None:
            return None
        return self.hl.ptr(self._addr + self.off_layer)
