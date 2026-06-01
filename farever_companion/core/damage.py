"""Read-only damage-event source via `ui.comp.DamageDisplay` (Phase 6).

Implements the competitor's technique WITHOUT hooking the allocator (a detour is
a code write). Each tick we enumerate the live `ui.comp.DamageDisplay` objects
(the floating numbers the game renders for YOUR client — already filtered to your
outgoing/visible damage), read each one's `st.skill.DamageResult`
{ skill, amount, crit, kill, target, kind }, dedupe by object identity over its
short life, and tag any whose target is our own hero as `incoming` (damage taken
/ DoT — fed to the DTPS + death-recap aggregate, never the outgoing headline).
The result feeds DpsMeter.add_event for per-(kind,skill) / crit / kill stats.

`kind` (damage|heal|shield) comes from `OFF_RESULT_KIND` once calibrated (an
enum/sign on DamageResult; see `KIND_MAP`). Until that offset is pinned every
event reads as `kind="damage"` — heals/shields simply don't separate out yet
(we never fabricate a heal value).

STATUS: the class is located by the same string→type→instances scan as the
player; the field OFFSETS below must be calibrated live (PLAN §7). Until then
`calibrated()` is False and `poll()` returns [] — so the meter cleanly uses the
HP-diff fallback and never emits fabricated events.
"""
from __future__ import annotations

import os
import struct
import sys
import time

from .hl import Hl, is_ptr, _HOBJ
from .proc import Proc, ProcError
from ..combat.dps import DamageEvent

# --- calibrated live 2026-05-30 (build 13,358,488 B; pid 10380) via HL type
# reflection (`_re_reflect.py`): read ui.comp.DamageDisplay -> st.skill.DamageResult
# field offsets straight from the runtime fields_indexes table (names preserved).
# These are struct field offsets (stable for the build); the type pointer itself
# is still located dynamically by name in `_find_type`. Re-run `_re_reflect.py`
# if a patch shifts the layout.
OFF_DISPLAY_RESULT: int | None = 0x498  # DamageDisplay.dmg -> st.skill.DamageResult
OFF_RESULT_AMOUNT: int | None = 0x50    # DamageResult._amount (f64)
OFF_RESULT_SKILL: int | None = 0x08     # DamageResult.baseSkill (-> st.skill.BaseSkill)
OFF_RESULT_CRIT: int | None = 0x69      # DamageResult._critical (bool, 1 byte)
OFF_RESULT_KILL: int | None = 0x68      # DamageResult._kill (bool, 1 byte)
OFF_RESULT_TARGET: int | None = 0x28    # DamageResult.target (-> ent.GameObject)
OFF_SKILL_ID: int | None = 0xA0         # BaseSkill.kind (String) — per-skill name
# No heal/shield discriminator on DamageResult — heals render via a separate
# display class (a HealDisplay sibling, not yet sourced). Leave kind=damage; never
# fabricate a heal. (See `_re_reflect.py` / PLAN Phase 0 RE log.)
OFF_RESULT_KIND: int | None = None      # DamageResult.kind enum (i32), or None

# Map the raw DamageResult.kind enum value -> our kind label. Fill this in when
# OFF_RESULT_KIND is calibrated (take a heal, gain a shield, note the value);
# any value not in the map degrades to "damage". See `_re_damage.py`.
KIND_MAP: dict[int, str] = {}

TO_NAME = 0x10

# --- cluster-scan tuning ----------------------------------------------------
# Pad each located instance into a window and merge nearby windows, so the per-
# tick scan covers the GC size-class pages where new DamageDisplays will spawn —
# not just the exact addresses that were live during the (slow) discovery scan.
# HashLink's GC pages are 64 KiB; these are deliberately generous (cluster spread
# is bounded by how many same-size pages the class occupies) but still tiny next
# to the ~23 GB heap. Tune from `_re_cluster.py`'s measured spread + scan time.
RANGE_PAD = 1 << 18          # 256 KiB each side (coverage comes from accumulated anchors)
RANGE_MERGE_GAP = 1 << 20    # coalesce instances whose padded windows are <1 MiB apart
MAX_SCAN_BYTES = 512 << 20   # safety cap: never let derived ranges exceed 512 MiB
MAX_ANCHORS = 8000           # bound on the accumulated anchor set


def cluster_ranges(addrs: list[int], pad: int = RANGE_PAD,
                   merge_gap: int = RANGE_MERGE_GAP) -> list[tuple[int, int]]:
    """Collapse a set of instance addresses into a small list of merged
    `(base, len)` scan ranges. Pure/testable. Relies on the GC being non-moving
    and size-class-segregated: padding the known addresses and merging neighbours
    covers the pages where future same-class objects appear."""
    pts = sorted(set(a for a in addrs if a > 0))
    if not pts:
        return []
    ranges: list[tuple[int, int]] = []
    lo = max(0, pts[0] - pad)
    hi = pts[0] + pad
    for a in pts[1:]:
        if a - pad <= hi + merge_gap:
            hi = max(hi, a + pad)
        else:
            ranges.append((lo, hi - lo))
            lo, hi = max(0, a - pad), a + pad
    ranges.append((lo, hi - lo))
    return ranges


def clip_ranges(ranges: list[tuple[int, int]],
                regions: list[tuple]) -> list[tuple[int, int]]:
    """Intersect scan ranges with committed readable `regions` (each `(base,
    size, ...)` from `Proc.regions()`) so a padded range never straddles an
    unmapped gap. Pure/testable."""
    if not ranges:
        return []
    regs = sorted((b, b + s) for b, s, *_ in regions)
    out: list[tuple[int, int]] = []
    for base, length in ranges:
        rs, re_ = base, base + length
        for rb, rend in regs:
            if rend <= rs:
                continue
            if rb >= re_:
                break
            a, b = max(rs, rb), min(re_, rend)
            if b > a:
                out.append((a, b - a))
    return out


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


class DamageReader:
    CLASS = "ui.comp.DamageDisplay"

    def __init__(self, proc: Proc, hl: Hl, my_hero: int | None = None):
        self.proc = proc
        self.hl = hl
        self.my_hero = my_hero
        self._type_ptr: int | None = None
        # optional override (e.g. FAREVER_DMG_TYPE=0x163ae3c97f8): skip the slow
        # heap scan when the type ptr is already known for this game session. It's
        # verified by class name before use, so a stale ptr falls back to the scan.
        self._type_hint: str | None = os.environ.get("FAREVER_DMG_TYPE")
        # Dedup by CONTENT signature per display, not by address: the non-moving
        # GC recycles freed DamageResult/DamageDisplay slots under sustained combat,
        # so a new number can reuse a just-freed address. An address-only "seen" set
        # would skip it (undercount → "only a few skills"). Keyed by DamageDisplay
        # addr -> last-emitted (res, amount, skill, crit, kill); emit on change.
        self._sig: dict[int, tuple] = {}
        # Cluster-scan state: HashLink's GC is non-moving and segregates objects by
        # size class into shared pages, so every live DamageDisplay (the persistent
        # ~20 AND each transient floating number) lives in the same handful of GC
        # pages. We locate that cluster once (slow full scan), then re-enumerate it
        # each tick by scanning ONLY those ranges (tens of MB, sub-100ms) — fast
        # enough to catch a number during its ~1s life. See DPS_METER_PLAN.
        self._scan_ranges: list[tuple[int, int]] = []
        self._ranges_ready = False
        self._last_count = 0
        self._anchor_addrs: set[int] = set()   # accumulated instance addrs (union of derives)

    @staticmethod
    def calibrated() -> bool:
        return None not in (OFF_DISPLAY_RESULT, OFF_RESULT_AMOUNT, OFF_RESULT_TARGET)

    def _log(self, msg: str) -> None:
        print(f"[dmg-scan] {msg}", file=sys.stderr, flush=True)

    def _find_type(self) -> int | None:
        if self._type_ptr is not None or not self.proc.has_scan:
            return self._type_ptr
        # 0) verified override — instant when the type ptr is known for the session
        if self._type_hint:
            try:
                cand = int(self._type_hint, 0)
                if self.hl.type_name(cand) == self.CLASS:
                    self._log(f"using FAREVER_DMG_TYPE hint {cand:#x}")
                    self._type_ptr = cand
                    return cand
                self._log("FAREVER_DMG_TYPE hint stale (class mismatch) — scanning")
            except (ProcError, OSError, ValueError):
                self._log("FAREVER_DMG_TYPE hint unreadable — scanning")
            self._type_hint = None
        # 1) locate the type by name. The class-name string may sit in read-only
        # const data, but every back-reference (type_obj.name, hl_type+8, and the
        # live instances) lives in the WRITABLE heap — so the two nested scans use
        # rw_only=True, which skips the read-only mapped regions.
        t0 = time.monotonic()
        names = self.proc.find_bytes(_utf16z(self.CLASS), align=2,
                                     rw_only=False, max_hits=64)
        self._log(f"name-string scan: {len(names)} hit(s) in {time.monotonic()-t0:.1f}s")
        for name_addr in names:
            refs = self.proc.find_qword(name_addr, rw_only=True, max_hits=64)
            for ref in refs:
                type_obj = ref - TO_NAME
                for tref in self.proc.find_qword(type_obj, rw_only=True, max_hits=64):
                    cand = tref - 8
                    try:
                        if self.hl.i32(cand) == _HOBJ and self.hl.type_name(cand) == self.CLASS:
                            self._log(f"located type {cand:#x} in {time.monotonic()-t0:.1f}s")
                            self._type_ptr = cand
                            return cand
                    except (ProcError, OSError):
                        continue
        self._log(f"type NOT found this pass ({time.monotonic()-t0:.1f}s)")
        return None

    def refresh_ranges(self) -> int:
        """SLOW (one ~heap sweep): locate the type, enumerate every live
        DamageDisplay, and derive the small set of GC-page ranges they cluster
        into. Call occasionally on a maintenance thread (NOT every tick) so the
        ranges keep up as the GC grows new size-class pages. `poll()` then scans
        only those ranges, cheaply. Returns the instance count found.

        Run it while damage is flowing (more instances alive -> a tighter, more
        representative cluster). It still works idle (the persistent ~20 instances
        pin the same pages), just with fewer anchor points."""
        if not self.calibrated():
            return 0
        tp = self._find_type()
        if tp is None:
            return 0
        try:
            insts = self.proc.find_qword(tp, rw_only=True, max_hits=4096)
        except (ProcError, OSError):
            return self._last_count
        # ACCUMULATE anchors across derives (union) — a later weak/lull derive can
        # only ADD coverage, never shrink it. The GC is non-moving and a page keeps
        # its size class for life, so old anchors stay valid; clip_ranges below drops
        # any page the GC has since freed+decommitted. This converges to COMPLETE
        # coverage of the size-class page chain and stays there (fixes the stale /
        # regressing cluster that silently dropped events mid-fight).
        self._anchor_addrs.update(insts)
        if len(self._anchor_addrs) > MAX_ANCHORS:
            self._anchor_addrs = set(sorted(self._anchor_addrs)[-MAX_ANCHORS:])
        anchors = sorted(self._anchor_addrs)
        ranges = cluster_ranges(anchors)
        # clip to committed regions (reads never span an unmapped gap; also prunes
        # freed pages) and enforce the safety cap by tightening the pad if needed.
        try:
            ranges = clip_ranges(ranges, self.proc.regions())
        except (ProcError, OSError):
            pass
        total = sum(length for _, length in ranges)
        if total > MAX_SCAN_BYTES:
            self._log(f"cluster {total >> 20} MiB > cap; tightening pad")
            ranges = clip_ranges(
                cluster_ranges(anchors, pad=RANGE_PAD >> 3, merge_gap=RANGE_MERGE_GAP >> 3),
                self._regions_or_empty(),
            )
        self._scan_ranges = ranges
        self._last_count = len(insts)
        self._ranges_ready = bool(ranges)
        self._log(f"ranges: {len(insts)} insts (+acc {len(anchors)}) -> {len(ranges)} "
                  f"range(s), {sum(l for _, l in ranges) >> 20} MiB")
        return len(insts)

    def _regions_or_empty(self) -> list[tuple]:
        try:
            return self.proc.regions()
        except (ProcError, OSError):
            return []

    def poll(self) -> list[DamageEvent]:
        """FAST (range-bounded scan): re-enumerate the live DamageDisplay objects
        by scanning ONLY the cluster ranges, then emit an event for each display
        whose shown number changed since last poll. Dedup is by a CONTENT signature
        (res, amount, skill, crit, kill) keyed per display — robust to the GC
        recycling slots mid-combat (a recycled address carries a new signature, so
        it still emits). [] until `refresh_ranges()` has run.

        Incoming hits (target == our hero) are NOT emitted here: damage TAKEN is
        derived from our own HP drops (model `_sample_player_hp`), which is reliable
        whether or not the game renders a DamageDisplay for incoming damage."""
        tp = self._type_ptr
        if tp is None or not self._scan_ranges:
            return []
        try:
            insts = self.proc.find_qword_in(tp, self._scan_ranges, max_hits=4096)
        except (ProcError, OSError):
            return []
        events: list[DamageEvent] = []
        cur: dict[int, tuple] = {}
        for disp in insts:
            try:
                res = self.hl.ptr(disp + OFF_DISPLAY_RESULT)
            except (ProcError, OSError):
                continue
            if not res:
                continue
            target = self.hl.ptr(res + OFF_RESULT_TARGET)
            if self.my_hero and target == self.my_hero:
                continue                       # incoming (taken) — HP-derived instead
            ev = self._read_event(res, target, False)
            # the cluster also holds the class singleton + freed slots whose
            # DamageResult is junk; a REAL hit has a sane amount + clean ASCII
            # skill id ("Mace_Base_Attack"), garbage decodes to huge numbers / CJK.
            if not self._plausible(ev):
                continue
            sig = (res, int(ev.amount), ev.skill, ev.crit, ev.kill)
            cur[disp] = sig
            if self._sig.get(disp) != sig:     # a NEW number on this display
                events.append(ev)
        # retain only currently-present displays so a later reuse re-emits
        self._sig = cur
        return events

    @staticmethod
    def _plausible(ev: DamageEvent) -> bool:
        s = ev.skill
        return (0.0 < ev.amount < 1e7
                and bool(s) and s != "?" and s.isascii() and s.isprintable())

    def _read_event(self, res: int, target: int | None,
                    incoming: bool = False) -> DamageEvent:
        amount = 0.0
        try:
            raw = self.proc.try_read(res + OFF_RESULT_AMOUNT, 8)
            amount = abs(struct.unpack("<d", raw)[0]) if raw else 0.0
            if amount > 1e9 or amount != amount:   # not an f64; try i32
                amount = float(self.hl.i32(res + OFF_RESULT_AMOUNT))
        except (ProcError, OSError):
            pass
        # Stale/pooled DamageDisplays carry amount<=0 and a garbage baseSkill ptr;
        # resolving its name can hit unmapped memory — guard so one bad slot can't
        # break the whole poll (the meter ignores amount<=0 anyway).
        skill = "?"
        if OFF_RESULT_SKILL is not None and OFF_SKILL_ID is not None:
            try:
                sk = self.hl.ptr(res + OFF_RESULT_SKILL)
                if sk:
                    skill = self.hl.hl_string(self.hl.ptr(sk + OFF_SKILL_ID)) or "?"
            except (ProcError, OSError):
                skill = "?"
        crit = bool(self._flag(res, OFF_RESULT_CRIT))
        kill = bool(self._flag(res, OFF_RESULT_KILL))
        kind = KIND_MAP.get(self._flag(res, OFF_RESULT_KIND), "damage")
        return DamageEvent(amount=amount, skill=skill, crit=crit, kill=kill,
                           target=str(target), kind=kind, incoming=incoming)

    def _flag(self, res: int, off: int | None) -> int:
        # _kill (+0x68) and _critical (+0x69) are adjacent single-byte bools, so
        # read ONE byte — an i32 read would bleed the neighbour into the value.
        if off is None:
            return 0
        raw = self.proc.try_read(res + off, 1)
        return raw[0] if raw else 0
