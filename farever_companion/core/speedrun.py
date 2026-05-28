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

    def __init__(self):
        self.state = self.READY
        self._t0 = 0.0
        self._frozen = 0.0
        self._boss_seen_alive = False
        self.boss_id: str | None = None     # boss tracked for this run (display)
        self.last: float | None = None       # last finished time (s)
        self.is_new_best = False             # set True on a finish that beat PB

    # --- control (hotkeys / buttons) ------------------------------------
    def start(self) -> None:
        self.state = self.RUNNING
        self._t0 = time.monotonic()
        self._frozen = 0.0
        self._boss_seen_alive = False
        self.is_new_best = False

    def stop(self) -> None:
        if self.state == self.RUNNING:
            self._frozen = time.monotonic() - self._t0
            self.state = self.DONE
            self.last = self._frozen

    def reset(self) -> None:
        self.state = self.READY
        self._frozen = 0.0
        self._boss_seen_alive = False
        self.boss_id = None
        self.is_new_best = False

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
    def feed_boss(self, boss_id: str | None, present: bool, hp: float | None) -> bool:
        """Update boss tracking for the active run. Returns True the moment the
        tracked boss dies (was seen alive, now gone or hp<=0) while running —
        the caller then knows a kill just auto-stopped the timer."""
        if self.state != self.RUNNING:
            return False
        if boss_id:
            self.boss_id = boss_id
        alive = present and (hp is None or hp > 0)
        if alive:
            self._boss_seen_alive = True
            return False
        # boss is gone or at/below 0 hp — only a kill if we saw it alive first
        if self._boss_seen_alive and (not present or (hp is not None and hp <= 0)):
            self.stop()
            return True
        return False
