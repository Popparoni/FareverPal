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

# chest visual states observed live: 'Closed' (available), 'Locked' (needs
# key/quest - still present, not looted). Anything else on a loaded chest
# element means it was opened; back to Closed (respawn) un-marks.
CHEST_AVAILABLE_STATES = frozenset({"Closed", "Locked"})


def sync_chests(live: list[tuple[str, str | None]], done: set[str]
                ) -> tuple[list[str], list[str]]:
    """`live` = [(chest_id, state)] for loaded chest elements. Returns
    (mark opened, unmark) deltas; unknown/null states change nothing.
    NOTE: an opened crate keeps reading 'Closed' (lazy visual state, observed
    live), so this only catches future state words; the reliable open signal
    is the LootDrop spawn below."""
    mark: list[str] = []
    unmark: list[str] = []
    for cid, state in live:
        if not state or not cid:
            continue
        if state in CHEST_AVAILABLE_STATES:
            if cid in done:
                unmark.append(cid)          # respawned / still lootable
        elif cid not in done:
            mark.append(cid)                # opened
    return mark, unmark


# drops scatter around the opened chest (observed live: nearest pile 4.4u
# from the chest origin); chests are never packed this close together
LOOTDROP_RADIUS = 8.0


def chests_opened_by_drops(drops: list[tuple[float, float, float]],
                           chests: list[tuple[str, float, float, float]],
                           done: set[str]) -> list[str]:
    """Opening a chest spawns LootDrop elements at its position - the only
    reliable client-side open signal (the chest's own state stays 'Closed').
    `drops` = loot-drop positions, `chests` = (id, x, y, z). Returns ids to
    mark opened. Only catches opens while the companion watches; despawned
    drops simply stop marking (the mark persists)."""
    mark: list[str] = []
    if not drops:
        return mark
    r2 = LOOTDROP_RADIUS * LOOTDROP_RADIUS
    for cid, cx, cy, cz in chests:
        if cid in done:
            continue
        for dx, dy, dz in drops:
            if ((cx - dx) ** 2 + (cy - dy) ** 2 + (cz - dz) ** 2) <= r2:
                mark.append(cid)
                break
    return mark


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
