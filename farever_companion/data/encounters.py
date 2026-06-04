"""Boss-encounter definitions for the boss-only speedrun split.

Decides, per dungeon, when the boss FIGHT starts (so the boss-only timer can arm)
while the run still ends on the same boss KILL the full-run timer already uses.

Most dungeons are a boss surrounded by trash, so the fight starts on the first hit
to the boss itself - that's the default (`{boss}`, kill on the boss), and the boss
split behaves like the single-boss timer.

A few dungeons are "trashless": the room holds only the boss and its adds and
nothing else, so the very first hit to ANYTHING is the fight starting. Those are
listed in `_ENGAGE_ANY` and arm on the first hit to any enemy - no need to
enumerate the adds. Cleodora ("Queen Honeyzabeth": the giant bee + her worker bees)
is the one such dungeon; the kill stays on Cleodora, which is detected today.
"""
from __future__ import annotations

# Kill-boss unit_ids whose room is trashless, so the boss split arms on the first
# hit to ANY enemy (not just the boss). Confirmed live: the Cleodora/Honeyzabeth
# room holds only the giant bee + her workers.
_ENGAGE_ANY: set[str] = {"Cleodora"}


def resolve(boss_id: str | None) -> tuple[set[str], str | None, bool]:
    """`(members, kill_id, engage_any)` for the encounter the given boss belongs to.

    - `members`: unit_ids that count as the fight (used when not engage_any).
    - `kill_id`: the unit whose death ends the run (the detected dungeon boss).
    - `engage_any`: arm the boss split on the first hit to ANY enemy (trashless
      rooms), so the adds don't need enumerating.
    Unknown / single bosses map to themselves with engage_any off.
    """
    if not boss_id:
        return (set(), None, False)
    return ({boss_id}, boss_id, boss_id in _ENGAGE_ANY)
