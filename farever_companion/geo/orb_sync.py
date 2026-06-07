"""Auto-detect collected secret orbs from the live scene.

World orbs never despawn client-side, and their `currentVisualState` flaps
with chunk activation - useless. The reliable signal (captured through a live
collection 2026-06-07): the glow-fx pointer. An uncollected orb carries
`currentFx` at any loaded distance (seen set at 260m); collecting it nulls
the fx instantly while the state stays 'Enabled'. So: collected = loaded
RedOrb_World element with no fx. Marking is debounced over two consecutive
observations (fx can spawn a tick late on chunk load); a glowing orb un-marks
immediately - live truth beats any stale mark. Pure logic; the caller feeds
observations and applies the returned deltas.
"""
from __future__ import annotations

LIVE_PREFIX = "RedOrb_World"


class FxSync:
    def __init__(self) -> None:
        self._pending: set[str] = set()   # fx-less once; confirm next tick

    def update(self, live: list[tuple[str, bool]], done: set[str]
               ) -> tuple[list[str], list[str]]:
        """`live` = [(orb_id, fx_present)] for loaded RedOrb_World elements,
        `done` = currently-marked-collected ids. Returns (mark, unmark)."""
        mark: list[str] = []
        unmark: list[str] = []
        seen_dark: set[str] = set()
        for oid, fx in live:
            if fx:
                self._pending.discard(oid)
                if oid in done:
                    unmark.append(oid)       # glowing = definitely not collected
            else:
                seen_dark.add(oid)
                if oid in done:
                    continue
                if oid in self._pending:
                    mark.append(oid)         # fx-less twice in a row
                else:
                    self._pending.add(oid)
        self._pending &= seen_dark           # drop pendings that unloaded
        return mark, unmark
