"""Pure-read player locator — no hook, no writes.

Locates the local player's `ent.Hero` struct entirely by reading + scanning,
using the Rust `find_bytes`. Strategy (write-free, see PLAN §4.2):

  1. Bootstrap the `ent.Hero` hl_type by string:
       a. scan for the UTF-16 class name "ent.Hero\\0"  -> name string addr
       b. scan for a pointer to that name              -> hl_type_obj (name @ +0x10)
       c. scan for a pointer to that type_obj          -> hl_type (obj @ +8), kind=HOBJ
     The type pointer is stable for the process, so it's cached; re-locating on a
     zone change only re-runs step 2.
  2. Scan rw heap for objects whose first qword == that hl_type  -> Hero instances.
  3. Select the local player: prefer `Hero.ownerPlayer.isMe == 1` verified with
     `Player.hero == Hero` (offsets calibrated live — constants below). Until
     calibrated, fall back to the hero with the largest live unit list (the
     loaded world), which is unambiguous in solo play.

Their external scan took ~1 min; the targeted 8-byte type match should be much
faster. Field offsets to calibrate live are isolated as constants.
"""
from __future__ import annotations

import struct

from .appsingleton import GameAppLocator
from .hl import Hl, is_ptr, _HOBJ
from .proc import Proc, ProcError
from .scene import OFF_GAMELAYER, OFF_UNITS_ARR

# --- calibrate live (PLAN §7); None => use the fallback heuristic ---------
OFF_HERO_OWNERPLAYER: int | None = None   # ent.Hero -> st.Player
OFF_PLAYER_ISME: int | None = None        # st.Player.isMe (i32/bool)
OFF_PLAYER_HERO: int | None = None        # st.Player.hero -> ent.Hero
OFF_HERO_ISCOMBAT: int | None = None      # ent.Hero.isInCombat (bool)

TO_NAME = 0x10   # hl_type_obj.name offset (matches hl.TO_NAME)

OFF_XYZ = 0x98       # x,y,z contiguous f64 in the player struct
OFF_HEADING = 0xB0   # facing angle, f64 radians (atan2: 0=+x, CCW), calibrated live


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


class PlayerLocator:
    def __init__(self, proc: Proc, hl: Hl | None = None):
        self.proc = proc
        self.hl = hl or Hl(proc)
        self._type_ptr: int | None = None
        self.address: int | None = None
        # Primary, scan-free anchor: the GameApp singleton -> GameApp.hero.
        # The Hero heap-scan below is kept as a fallback if the anchor can't be
        # resolved (e.g. an unexpected build layout).
        self.app = GameAppLocator(proc, self.hl)

    # --- type bootstrap --------------------------------------------------
    def hero_type(self) -> int | None:
        if self._type_ptr is not None:
            return self._type_ptr
        if not self.proc.has_scan:
            raise ProcError("player locate needs the farever_native scan backend")
        name_hits = self.proc.find_bytes(_utf16z("ent.Hero"), align=2,
                                         rw_only=False, max_hits=64)
        for name_addr in name_hits:
            # hl_type_obj.name points here -> type_obj = (ptr to name_addr) - TO_NAME
            for ref in self.proc.find_qword(name_addr, rw_only=False, max_hits=64):
                type_obj = ref - TO_NAME
                # a hl_type points to type_obj at +8, with kind=HOBJ at +0
                for tref in self.proc.find_qword(type_obj, rw_only=False, max_hits=64):
                    cand = tref - 8
                    try:
                        if self.hl.i32(cand) == _HOBJ and self.hl.type_name(cand) == "ent.Hero":
                            self._type_ptr = cand
                            return cand
                    except ProcError:
                        continue
        return None

    # --- instance scan + selection --------------------------------------
    def _candidates(self) -> list[int]:
        tp = self.hero_type()
        if tp is None:
            return []
        hits = self.proc.find_qword(tp, rw_only=True, max_hits=256)
        # keep ones that are real, fully-formed heroes (valid GameLayer)
        good = []
        for h in hits:
            gl = self.hl.ptr(h + OFF_GAMELAYER)
            if gl is not None:
                good.append(h)
        return good

    def _is_me(self, hero: int) -> bool | None:
        """True/False if the isMe path is calibrated, else None (unknown)."""
        if OFF_HERO_OWNERPLAYER is None or OFF_PLAYER_ISME is None:
            return None
        player = self.hl.ptr(hero + OFF_HERO_OWNERPLAYER)
        if player is None:
            return False
        try:
            isme = self.hl.i32(player + OFF_PLAYER_ISME) == 1
        except ProcError:
            return False
        if OFF_PLAYER_HERO is not None:
            back = self.hl.ptr(player + OFF_PLAYER_HERO)
            if back != hero:
                return False
        return isme

    def _unit_count(self, hero: int) -> int:
        gl = self.hl.ptr(hero + OFF_GAMELAYER)
        if gl is None:
            return -1
        arr = self.hl.ptr(gl + OFF_UNITS_ARR)
        if arr is None:
            return -1
        try:
            return self.hl.i32(arr + 8)
        except ProcError:
            return -1

    def locate(self) -> int | None:
        # Primary: GameApp singleton -> hero (scan-free, session-stable, and
        # unambiguous — GameApp.hero IS the local player).
        try:
            hero = self.app.hero()
        except ProcError:
            hero = None
        if hero is not None and self.hl.class_of(hero) == "ent.Hero":
            self.address = hero
            return hero
        # Fallback: scan the heap for ent.Hero and disambiguate (old method).
        cands = self._candidates()
        if not cands:
            self.address = None
            return None
        # calibrated path: the verified local player
        verified = [h for h in cands if self._is_me(h) is True]
        if verified:
            self.address = verified[0]
            return self.address
        # fallback: the hero attached to the most populated world
        self.address = max(cands, key=self._unit_count)
        return self.address

    def relocate(self) -> int | None:
        """Re-find the instance after a zone change (type stays cached)."""
        return self.locate()

    # --- reads -----------------------------------------------------------
    def pbase(self) -> int | None:
        return self.address

    def read_xyz(self) -> tuple[float, float, float] | None:
        if not self.address:
            return None
        raw = self.proc.try_read(self.address + OFF_XYZ, 24)
        if raw is None:
            return None
        return struct.unpack("<ddd", raw)

    def read_heading(self) -> float | None:
        """Player facing in radians (atan2: 0=+x, CCW). Pure read of the heading
        field in the player struct; holds when still, updates while turning."""
        if not self.address:
            return None
        raw = self.proc.try_read(self.address + OFF_HEADING, 8)
        if raw is None:
            return None
        return struct.unpack("<d", raw)[0]

    def in_combat(self) -> bool | None:
        if not self.address or OFF_HERO_ISCOMBAT is None:
            return None
        try:
            return self.hl.i32(self.address + OFF_HERO_ISCOMBAT) != 0
        except ProcError:
            return None
