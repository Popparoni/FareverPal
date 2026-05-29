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
