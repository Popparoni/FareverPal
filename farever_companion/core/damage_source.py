"""Per-skill DamageDisplay source: lifecycle + calibration (opt-in, experimental).

Owns the DamageReader, the slow background type-locate + GC-cluster mapping, the
off-thread poller, and the per-skill event meter (self.dps_events). LiveModel
keeps the always-on HP-diff meter and feeds this a per-tick in-combat signal.
Gated behind the experimental flag; HP-diff is the fallback when uncalibrated.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque

from .hl import Hl
from .proc import ProcError
from ..combat.dps import DpsMeter

log = logging.getLogger(__name__)


class DamageSourceManager:
    def __init__(self, proc):
        self.proc = proc
        self.dps_events = DpsMeter()       # per-skill DamageDisplay events -> Skill panel
        self._damage = None                # lazily created DamageReader
        self._damage_scan_started = False  # the type-locate runs once, off-thread
        self._damage_scan_t0 = 0.0         # when calibration started (for progress UI)
        self._redrive = threading.Event()  # combat-triggered cluster re-derive request
        self._combat_q: deque = deque(maxlen=20000)   # bg poller -> UI drain
        self._poller_started = False
        self._stop = False                 # signals background threads to exit
        try:
            from ..config import Settings, experimental_enabled
            self.per_skill_enabled = bool(experimental_enabled()
                                          and Settings.load().dps_per_skill)
        except Exception as e:
            log.debug("per-skill settings unreadable; defaulting OFF: %s", e)
            self.per_skill_enabled = False

    # --- lifecycle -------------------------------------------------------
    def warmup(self, player_addr: int | None) -> None:
        """Kick off the (slow, ~1 min) DamageDisplay type-locate in the
        background so per-skill DPS is ready by the time the meter is used."""
        try:
            self._source(player_addr)
        except Exception as e:
            log.debug("per-skill damage-source warmup skipped: %s", e)

    def shutdown(self) -> None:
        self._stop = True

    def reset(self) -> None:
        self.dps_events.reset()

    # --- status / progress (drive the Skills panel hints) ----------------
    def status(self) -> str:
        """'active' | 'scanning' | 'off', drives the meter's 'calibrating' hint.
        'off' = uncalibrated or no scan backend; 'scanning' = the type-locate /
        cluster mapping is running; 'active' = the cluster ranges are ready."""
        from .damage import DamageReader
        if not DamageReader.calibrated() or not self.proc.has_scan:
            return "off"
        if self._damage is not None and getattr(self._damage, "_ranges_ready", False):
            return "active"
        return "scanning" if self._damage_scan_started else "off"

    def progress(self) -> tuple[str, float]:
        """Calibration progress for the Skills panel: (phase_label, fraction 0..1).
        Returns ('', 1.0) once ready and ('', 0.0) when per-skill is off. The
        fractions are elapsed-time estimates (the Rust scans give no real %), so
        the bar advances steadily and caps just shy of full until each phase ends.
        Phase 1 = locate the DamageDisplay type (~full-heap name scan, the slow
        one); phase 2 = map the GC cluster ranges (a heap sweep for instances)."""
        from .damage import DamageReader
        if (not self.per_skill_enabled or not DamageReader.calibrated()
                or not self.proc.has_scan):
            return ("", 0.0)
        d = self._damage
        if d is None or not self._damage_scan_started:
            return ("starting…", 0.02)
        if getattr(d, "_ranges_ready", False):
            return ("", 1.0)
        el = time.monotonic() - (self._damage_scan_t0 or time.monotonic())
        if d._type_ptr is None:
            return (f"locating damage type · {el:.0f}s", min(0.60, 0.05 + el / 150.0))
        return (f"mapping — keep attacking · {el:.0f}s", min(0.96, 0.62 + el / 100.0))

    def recalibrate(self) -> None:
        """User-triggered: re-map the per-skill cluster NOW (attack while it runs).
        Wakes the maintenance thread to re-derive the GC ranges against the live
        DamageDisplay instances on screen, so a cluster that mapped during a lull
        (no numbers) is fixed without waiting for the auto self-heal. No-op until
        per-skill is enabled + the type is located."""
        self._start_type_scan()      # ensure the maintenance thread exists
        self._redrive.set()

    # --- the reader + its background threads -----------------------------
    def _source(self, player_addr: int | None):
        """The per-skill DamageDisplay reader, OPT-IN only (`per_skill_enabled`).
        Returns the reader once its cluster ranges are derived (else None -> the
        caller stays on HP-diff in the meantime)."""
        if not self.per_skill_enabled:
            return None
        from .damage import DamageReader
        if not DamageReader.calibrated() or not self.proc.has_scan:
            return None
        if self._damage is None:
            # dedicated Hl so the background poller's reflection caches don't race
            # with the UI thread's model.hl.
            self._damage = DamageReader(self.proc, Hl(self.proc), player_addr)
        self._damage.my_hero = player_addr     # for incoming-damage tagging
        self._start_type_scan()
        return self._damage if getattr(self._damage, "_ranges_ready", False) else None

    def _start_type_scan(self) -> None:
        # ONE maintenance thread (the flag is never cleared): locate the type, then
        # periodically re-derive the cluster ranges (a slow full heap sweep) so they
        # track the GC as it grows new size-class pages. The fast per-tick range
        # scans happen in the poller. Only runs while per-skill is enabled (exp).
        if self._damage_scan_started:
            return
        self._damage_scan_started = True
        self._damage_scan_t0 = time.monotonic()

        def _maintain():
            while not self._stop and self.per_skill_enabled:
                n = 0
                try:
                    n = self._damage.refresh_ranges()  # full sweep; derives _scan_ranges
                except Exception as e:
                    log.debug("per-skill range re-derive failed this pass: %s", e)
                self._redrive.clear()
                # Adaptive spacing. A derive during a LULL finds only the persistent
                # ~handful of instances, so the cluster may miss size-class pages that
                # only fill during combat -> re-derive soon. A healthy combat-time
                # derive (many instances) anchors those pages -> relax to ~5 min. AND
                # wake immediately if combat signals the cluster caught nothing
                # (`_redrive`), so a lull-derived cluster self-heals the moment you fight.
                interval = 45 if n < 16 else 300
                waited = 0.0
                while waited < interval:
                    if self._stop or not self.per_skill_enabled:
                        return
                    if self._redrive.wait(timeout=1.0):
                        break                  # combat asked for a fresh derive
                    waited += 1.0
        threading.Thread(target=_maintain, daemon=True, name="dmg-maintain").start()

    def _ensure_poller(self, src) -> None:
        """Run `src.poll()` on a BACKGROUND thread, feeding events into a queue
        the UI thread drains. poll() is a RANGE-BOUNDED scan (only the GC
        size-class pages the cluster occupies, ~tens of MB -> sub-100ms), not a
        full-heap sweep, but it still lives off the UI thread so a slow read can
        never stall a frame, and so it can poll fast enough to catch ~1s numbers."""
        if self._poller_started:
            return
        self._poller_started = True

        def _log(m):
            print(f"[dmg-poller] {m}", file=sys.stderr, flush=True)

        def _loop():
            _log(f"started; type_ptr={getattr(src, '_type_ptr', None)}")
            last_ev = 0.0
            total = 0
            polls = 0
            last_report = time.monotonic()
            while not self._stop:
                t0 = time.monotonic()
                try:
                    evs = src.poll()
                except (ProcError, OSError) as e:
                    _log(f"poll read error: {e}")
                    evs = []
                except Exception as e:
                    _log(f"poll EXC: {type(e).__name__}: {e}")
                    evs = []
                dt = time.monotonic() - t0
                for ev in evs:
                    self._combat_q.append(ev)
                total += len(evs)
                polls += 1
                now2 = time.monotonic()
                if now2 - last_report > 2.0:
                    _log(f"polls={polls} last={len(evs)}ev/{dt:.1f}s total={total} q={len(self._combat_q)}")
                    last_report = now2
                # Poll FAST throughout a fight: a number lives ~1s, so 0.08s polling
                # catches each one ~12x (never missed) as long as the cluster covers
                # it. The OLD idle ramp-up to 2.5s was the stutter cause - it slept
                # through the start of the next burst, dropping skills. poll() is
                # cheap now (range-bounded), so stay tight while events flowed within
                # the last 3s, and only back off after a real lull (saves CPU).
                if evs:
                    last_ev = now2
                in_combat = (now2 - last_ev) < 3.0
                time.sleep(0.08 if in_combat else 0.5)
        threading.Thread(target=_loop, daemon=True, name="dmg-poller").start()

    # --- per-tick sample (called from LiveModel.sample_combat) -----------
    def sample(self, player_addr: int | None, hp_in_combat: bool) -> None:
        """Drain the poller's per-skill events into `dps_events` (a SEPARATE meter
        so it never double-counts the HP-diff total). Self-heal: clearly in combat
        (HP-diff sees damage) yet the event source caught nothing -> the cluster was
        mapped during a lull and misses the live-number GC pages; ask the
        maintenance thread to re-derive NOW (a combat-time derive anchors them)."""
        src = self._source(player_addr)
        if src is None:
            return
        self._ensure_poller(src)
        drained = 0
        while self._combat_q and drained < 10000:
            self.dps_events.add_event(self._combat_q.popleft())
            drained += 1
        if hp_in_combat and not self.dps_events.in_combat:
            self._redrive.set()
