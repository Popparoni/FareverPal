"""Speedrun timer + dungeon-boss-kill watcher.

A tiny, process-free state machine. The overlay drives it: it ticks the elapsed
time for display and feeds the tracked boss's liveness each poll so the run can
auto-stop the instant the dungeon boss dies (was alive, now dead/gone).

States: ready -> running -> done. `toggle()` is the one-key start/stop.
"""
from __future__ import annotations

import time


def fmt_time(secs: float) -> str:
    """`mm:ss.cs` (centiseconds) — the speedrun-readable format."""
    secs = max(0.0, secs)
    m = int(secs // 60)
    s = secs - m * 60
    return f"{m:02d}:{s:05.2f}"


class SpeedrunTimer:
    READY, RUNNING, DONE = "ready", "running", "done"

    # feed_boss outcomes
    ALIVE, KILL, LEFT = "alive", "kill", "left"

    # A vanished boss counts as a KILL only if its last-seen HP was within this
    # fraction of the max HP we observed (i.e. it was at death's door). A boss
    # that disappears while still healthy means the PLAYER left — leaving the
    # dungeon / going to the main menu despawns the boss at full HP — so that is
    # an aborted run, never a kill. (Bias is deliberately toward missing an
    # auto-stop, which the user can finish by hand, over uploading a non-kill.)
    KILL_HP_FRAC = 0.05

    def __init__(self):
        self.state = self.READY
        self._t0 = 0.0
        self._frozen = 0.0
        self._boss_seen_alive = False
        self._last_hp: float | None = None    # boss HP at its last in-scene read
        self._max_hp = 0.0                     # max boss HP observed this run
        self.boss_id: str | None = None     # boss tracked for this run (display)
        self.last: float | None = None       # last finished time (s)
        self.is_new_best = False             # set True on a finish that beat PB
        self.is_kill = False                 # True iff this finish was a real boss kill

    # --- control (hotkeys / buttons) ------------------------------------
    def start(self) -> None:
        self.state = self.RUNNING
        self._t0 = time.monotonic()
        self._frozen = 0.0
        self._boss_seen_alive = False
        self._last_hp = None
        self._max_hp = 0.0
        self.is_new_best = False
        self.is_kill = False

    def stop(self, kill: bool = False) -> None:
        if self.state == self.RUNNING:
            self._frozen = time.monotonic() - self._t0
            self.state = self.DONE
            self.last = self._frozen
            self.is_kill = kill

    def reset(self) -> None:
        self.state = self.READY
        self._frozen = 0.0
        self._boss_seen_alive = False
        self._last_hp = None
        self._max_hp = 0.0
        self.boss_id = None
        self.is_new_best = False
        self.is_kill = False

    def toggle(self) -> None:
        """One-key: start when idle/finished, stop while running."""
        if self.state == self.RUNNING:
            self.stop()
        else:
            self.start()

    # --- query -----------------------------------------------------------
    def elapsed(self) -> float:
        if self.state == self.RUNNING:
            return time.monotonic() - self._t0
        return self._frozen

    # --- auto-stop on boss kill -----------------------------------------
    def feed_boss(self, boss_id: str | None, present: bool, hp: float | None) -> str:
        """Update boss tracking for the active run. Returns:
          KILL  — the tracked boss just died (auto-stops the timer, kill=True),
          LEFT  — the boss vanished while still healthy ⇒ the player left the
                  dungeon / hit the main menu; the run must be cancelled, NOT
                  uploaded,
          ALIVE — nothing conclusive yet (keep running).
        Only ALIVE/KILL change the timer here; the caller handles LEFT."""
        if self.state != self.RUNNING:
            return self.ALIVE
        if boss_id:
            self.boss_id = boss_id
        if present:
            self._boss_seen_alive = True
            if hp is not None:
                if hp <= 0:                       # died in scene (corpse at 0 HP)
                    self.stop(kill=True)
                    return self.KILL
                self._last_hp = hp
                self._max_hp = max(self._max_hp, hp)
            return self.ALIVE
        # boss not present (despawned). Nothing to conclude unless we saw it alive.
        if not self._boss_seen_alive:
            return self.ALIVE
        # Despawned after being tracked: a KILL only if it was near death when
        # last seen; otherwise the player left (full/high HP) — abort, no upload.
        if (self._last_hp is not None and self._max_hp > 0
                and self._last_hp <= self._max_hp * self.KILL_HP_FRAC):
            self.stop(kill=True)
            return self.KILL
        return self.LEFT


class AutoStarter:
    """Decides when a run auto-starts: pure, headless, unit-testable.

    Fed `(in_dungeon, pos)` once per overlay tick, it returns True exactly once —
    on the tick the player makes *real* movement away from a settled spawn
    baseline. Separated from the overlay (which only does I/O) so the start
    behaviour can be tested without the game or Qt.

    Why a settle gate: arriving in a dungeon teleports the player in, and the
    position can jitter for a few ticks while the scene loads/the camera settles.
    The old logic baselined the very first in-dungeon position, so that settling
    jitter could exceed the move threshold and trip the timer "on enter" some
    runs but not others (the inconsistency Hooch reported). We instead wait until
    the player has been essentially stationary for a few consecutive ticks, then
    baseline *that* resting spawn point — so only deliberate walking starts the
    run, every time.
    """

    SETTLE_EPS = 0.6        # per-tick move under this == "standing still"
    SETTLE_TICKS = 3        # consecutive still ticks before the baseline is trusted
    MOVE_THRESHOLD = 2.0    # distance from the baseline that counts as "the run began"
    TELEPORT_STEP = 50.0    # a single-tick jump bigger than this == load/teleport, not walking

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._base = None       # settled spawn position (baseline) once trusted
        self._last = None       # previous tick position
        self._settle = 0        # consecutive "standing still" ticks

    @staticmethod
    def _dist(a, b) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5

    def feed(self, in_dungeon: bool, pos) -> bool:
        """Return True on the tick a real run-start movement is detected."""
        # Outside a dungeon (or no position yet) → disarm; never start in town.
        if not in_dungeon or not pos:
            self.reset()
            return False
        if self._last is None:
            self._last = pos
            self._settle = 0
            return False
        step = self._dist(pos, self._last)
        self._last = pos
        # A big single-tick jump is a scene load / teleport-in, not walking:
        # throw away any tentative baseline and wait for things to settle.
        if step > self.TELEPORT_STEP:
            self._base = None
            self._settle = 0
            return False
        if self._base is None:
            # Only trust a baseline once the player has held still for a few
            # ticks — that's the real resting spawn point, free of load jitter.
            if step < self.SETTLE_EPS:
                self._settle += 1
                if self._settle >= self.SETTLE_TICKS:
                    self._base = pos
            else:
                self._settle = 0
            return False
        # Baseline established → deliberate movement away from it starts the run.
        if self._dist(pos, self._base) >= self.MOVE_THRESHOLD:
            self.reset()
            return True
        return False


class ModeLatch:
    """Latches the last confidently auto-detected difficulty *during* a run.

    The Normal↔Hard mixups Hooch saw came from resolving difficulty at the
    finish frame — the exact moment the boss dies/despawns, when its level is
    flaky or unreadable, so it fell back to the manual selector (often the wrong
    one). The boss's level is reliable while it's alive and fighting, so we
    observe it every tick of the run and remember the last good *auto* read. At
    finish we prefer that latched value over a fresh (possibly empty) read.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.mode: str | None = None
        self.src: str | None = None

    def observe(self, mode: str | None, src: str | None) -> None:
        """Record a live reading; only confident auto reads are latched."""
        if src == "auto" and mode in ("normal", "hard"):
            self.mode, self.src = mode, "auto"

    def resolve(self, fresh_mode: str | None, fresh_src: str | None) -> tuple[str | None, str | None]:
        """Final (mode, src): a fresh auto read wins; else the latched auto read;
        else the fresh fallback (manual selector / default)."""
        if fresh_src == "auto":
            return fresh_mode, fresh_src
        if self.src == "auto":
            return self.mode, "auto"
        return fresh_mode, fresh_src
