"""Read-only damage-event source via `ui.comp.DamageDisplay` (Phase 6).

Implements the competitor's technique WITHOUT hooking the allocator (a detour is
a code write). Each tick we enumerate the live `ui.comp.DamageDisplay` objects
(the floating numbers the game renders for YOUR client — already filtered to your
outgoing/visible damage), read each one's `st.skill.DamageResult`
{ skill, amount, crit, kill, target }, dedupe by object identity over its short
life, and drop any whose target is our own hero (incoming/DoT). The result feeds
DpsMeter.add_event for per-skill / crit / kill stats.

STATUS: the class is located by the same string→type→instances scan as the
player; the field OFFSETS below must be calibrated live (PLAN §7). Until then
`calibrated()` is False and `poll()` returns [] — so the meter cleanly uses the
HP-diff fallback and never emits fabricated events.
"""
from __future__ import annotations

import struct

from .hl import Hl, is_ptr, _HOBJ
from .proc import Proc, ProcError
from ..combat.dps import DamageEvent

# --- calibrate live (PLAN §7) --------------------------------------------
OFF_DISPLAY_RESULT: int | None = None   # DamageDisplay -> st.skill.DamageResult
OFF_RESULT_AMOUNT: int | None = None    # DamageResult.amount (f64 or i32)
OFF_RESULT_SKILL: int | None = None     # DamageResult.skill  (-> BaseSkill, has id String)
OFF_RESULT_CRIT: int | None = None      # DamageResult.crit (bool/i32)
OFF_RESULT_KILL: int | None = None      # DamageResult.kill (bool/i32)
OFF_RESULT_TARGET: int | None = None    # DamageResult.target (-> unit)
OFF_SKILL_ID: int | None = None         # BaseSkill -> id String

TO_NAME = 0x10


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


class DamageReader:
    CLASS = "ui.comp.DamageDisplay"

    def __init__(self, proc: Proc, hl: Hl, my_hero: int | None = None):
        self.proc = proc
        self.hl = hl
        self.my_hero = my_hero
        self._type_ptr: int | None = None
        self._seen: set[int] = set()        # DamageResult addresses already counted

    @staticmethod
    def calibrated() -> bool:
        return None not in (OFF_DISPLAY_RESULT, OFF_RESULT_AMOUNT, OFF_RESULT_TARGET)

    def _find_type(self) -> int | None:
        if self._type_ptr is not None or not self.proc.has_scan:
            return self._type_ptr
        for name_addr in self.proc.find_bytes(_utf16z(self.CLASS), align=2,
                                              rw_only=False, max_hits=64):
            for ref in self.proc.find_qword(name_addr, rw_only=False, max_hits=64):
                type_obj = ref - TO_NAME
                for tref in self.proc.find_qword(type_obj, rw_only=False, max_hits=64):
                    cand = tref - 8
                    try:
                        if self.hl.i32(cand) == _HOBJ and self.hl.type_name(cand) == self.CLASS:
                            self._type_ptr = cand
                            return cand
                    except ProcError:
                        continue
        return None

    def poll(self) -> list[DamageEvent]:
        """Newly-seen outgoing damage events since the last poll. [] until the
        offsets are calibrated live."""
        if not self.calibrated():
            return []
        tp = self._find_type()
        if tp is None:
            return []
        events: list[DamageEvent] = []
        for disp in self.proc.find_qword(tp, rw_only=True, max_hits=256):
            res = self.hl.ptr(disp + OFF_DISPLAY_RESULT)
            if not res or res in self._seen:
                continue
            target = self.hl.ptr(res + OFF_RESULT_TARGET)
            if self.my_hero and target == self.my_hero:
                continue                                  # incoming / DoT — drop
            self._seen.add(res)
            events.append(self._read_event(res, target))
        # bound the seen-set so it can't grow forever
        if len(self._seen) > 4096:
            self._seen = set(list(self._seen)[-2048:])
        return events

    def _read_event(self, res: int, target: int | None) -> DamageEvent:
        amount = 0.0
        try:
            raw = self.proc.try_read(res + OFF_RESULT_AMOUNT, 8)
            amount = abs(struct.unpack("<d", raw)[0]) if raw else 0.0
            if amount > 1e9 or amount != amount:   # not an f64; try i32
                amount = float(self.hl.i32(res + OFF_RESULT_AMOUNT))
        except ProcError:
            pass
        skill = "?"
        if OFF_RESULT_SKILL is not None and OFF_SKILL_ID is not None:
            sk = self.hl.ptr(res + OFF_RESULT_SKILL)
            if sk:
                skill = self.hl.hl_string(self.hl.ptr(sk + OFF_SKILL_ID)) or "?"
        crit = bool(self._flag(res, OFF_RESULT_CRIT))
        kill = bool(self._flag(res, OFF_RESULT_KILL))
        return DamageEvent(amount=amount, skill=skill, crit=crit, kill=kill,
                           target=str(target))

    def _flag(self, res: int, off: int | None) -> int:
        if off is None:
            return 0
        try:
            return self.hl.i32(res + off)
        except ProcError:
            return 0
