"""Per-unit loot: CDB unit-id -> drop table -> predicted drops, plus boss
detection.

Chain (verified against the CDB):
    unit.json      id    -> type (e.g. "Kobold"), lvl, maxLvl, faction, flags
    unitType.json  type  -> lootTable (e.g. "Kobold"); None for non-droppers
    lootTable.json       -> recursive expansion (loot.predict)

Boss detection (broadened vs the old tool, which only matched id==table):
  1. NAMED bosses: a unit whose id also names a loot table (MunsterChuck unit
     -> MunsterChuck table). Their signature drops live there, not in the
     generic type table. (10 of these.)
  2. FLAGS bit: bosses/elites carry a flag bit in unit.flags. The exact bit
     needs live calibration; BOSS_FLAG_BIT stays None until then, and this path
     is a no-op so we never mislabel.

Determinism: every function here is PURE (depends only on the CDB), so a given
unit-id always resolves to the same table — the source of the old "loot shows
inconsistently" bug was the *live* layer, not this. See core/model.py.
"""
from __future__ import annotations

from functools import lru_cache

from . import cdb, loot

# The bit in unit.flags that marks a boss. Calibrated from the CDB: exactly 13 of
# 403 units carry 0x10 — all 10 named bosses PLUS Phrixes/PhrixesP1/Ulserous
# (bosses whose id doesn't name a loot table, so the named-list missed them), and
# zero trash mobs. So 0x10 cleanly identifies bosses and broadens detection beyond
# the named list.
BOSS_FLAG_BIT: int | None = 0x10


@lru_cache(maxsize=1)
def _units_by_id() -> dict[str, dict]:
    return cdb.by_id("unit")


@lru_cache(maxsize=1)
def _type_to_table() -> dict[str, str | None]:
    return {r["id"]: r.get("lootTable") for r in cdb.lines("unitType")}


@lru_cache(maxsize=1)
def _named_bosses() -> frozenset[str]:
    """Units whose id also names a loot table (the boss convention)."""
    table_ids = {r["id"] for r in cdb.lines("lootTable")}
    return frozenset(u for u in _units_by_id() if u in table_ids)


def named_bosses() -> frozenset[str]:
    return _named_bosses()


def is_boss(unit_id: str | None) -> bool:
    """A named boss (has a signature loot table), or flagged as one once the
    boss flag bit is calibrated."""
    if not unit_id:
        return False
    if unit_id in _named_bosses():
        return True
    if BOSS_FLAG_BIT is not None:
        row = _units_by_id().get(unit_id)
        flags = row.get("flags") if row else None
        if isinstance(flags, int) and (flags & BOSS_FLAG_BIT):
            return True
    return False


def boss_loot_table(unit_id: str | None) -> str | None:
    """The boss's signature table (same id as the unit), or None."""
    return unit_id if (unit_id and unit_id in _named_bosses()) else None


def unit_info(unit_id: str | None) -> dict | None:
    """{type, lvl, maxLvl, faction, lootTable, flags} for a unit-id, or None."""
    if not unit_id:
        return None
    row = _units_by_id().get(unit_id)
    if row is None:
        return None
    utype = row.get("type")
    return {
        "type": utype,
        "lvl": row.get("lvl"),
        "maxLvl": row.get("maxLvl"),
        "faction": row.get("faction"),
        "flags": row.get("flags"),
        "lootTable": _type_to_table().get(utype) if utype else None,
    }


def loot_table_for_unit(unit_id: str | None) -> str | None:
    """Boss signature table if it's a named boss, else the type's trash table."""
    bt = boss_loot_table(unit_id)
    if bt:
        return bt
    info = unit_info(unit_id)
    return info.get("lootTable") if info else None


def predict_unit(unit_id: str, level: int | None = None
                 ) -> list[tuple[str, float, str, str]]:
    """Predicted drops for a unit-id at `level` (defaults to the unit's lvl)."""
    table = loot_table_for_unit(unit_id)
    if not table:
        return []
    info = unit_info(unit_id)
    lvl = level if level is not None else ((info.get("lvl") if info else None) or 1)
    return loot.predict_sorted(table, lvl)
