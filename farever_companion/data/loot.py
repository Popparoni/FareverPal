"""Loot predictor — recursive expansion of a loot table to expected per-item
drop probability.

Two entry kinds, selected by the table's `flags` (CDB column
`lootTable.flags`, a TFlags{Weights=1, WithAffinity=2}):

* **Independent-roll table (Weights flag clear).** Each entry rolls on its
  own: an item entry drops with probability `proba`; a sub-table entry rolls
  the sub-table with probability `proba`. Probabilities accumulate additively
  and a table can yield several (or zero) items. This is the default and
  covers trash mobs, crates and currency tables.

* **Weighted pick-one table (Weights flag set).** The engine makes a single
  *normalized* weighted pick over the eligible entries — `proba` is a relative
  weight, not an absolute chance — and always yields exactly one outcome (see
  `genLootTable(id, level, autoPick, count=1)` + `pickWeightNorm` in the
  bytecode; findings/formulas.md §2). So MunsterChuck's two 0.01-weight weapons
  are 50%/50% of a guaranteed weapon, not 1%/1%. Every boss's `bossLootTable`
  is a Weights table — a boss always drops a weapon; the rarity is rolled on
  top (see data/rarity.py) and the genuinely-rare cosmetic lives in the boss's
  separate (non-Weights) `lootTable`.

This corrects the earlier port (Farever.CT entry 1337094962) which never
normalized Weights tables — it matched the CT's internal predict step but not
observed drops. See findings/drops.md §"How drops work".

Pure data: needs no attached process.
"""
from __future__ import annotations

from functools import lru_cache

from . import cdb

MAX_DEPTH = 8
WEIGHTS_FLAG = 1   # lootTable.flags bit 0 (TFlags{Weights, WithAffinity})


@lru_cache(maxsize=1)
def _tables() -> dict[str, dict]:
    return {r["id"]: r for r in cdb.lines("lootTable")}


@lru_cache(maxsize=1)
def _items() -> dict[str, dict]:
    return {r["id"]: r for r in cdb.lines("item")}


def predict(
    tid: str,
    level: int,
    conds: int | None = None,
    _depth: int = 0,
    _visited: frozenset[str] | None = None,
) -> dict[str, float]:
    """Expected per-item drop probability for loot table `tid` at `level`.

    `conds`: optional TFlags bitmask {BasicFoe=1, SpecialFoe=2, DungeonFoe=4};
    an entry's conds bits must all be set in `conds` to be eligible. None
    (default) applies no condition filter (the in-game default).
    """
    if _depth > MAX_DEPTH:
        return {}
    _visited = _visited or frozenset()
    if tid in _visited:
        return {}
    _visited = _visited | {tid}

    t = _tables().get(tid)
    if t is None:
        return {f"<missing: {tid}>": 1.0}

    # Entries surviving the level/conds gate are the candidates the engine
    # actually sees; for a Weights table the normalization sum is over these.
    eligible = [e for e in t.get("loot", []) if _entry_ok(e, level, conds)]

    weighted = bool((t.get("flags") or 0) & WEIGHTS_FLAG)
    if weighted:
        total = sum(e.get("proba", 0) for e in eligible)
        if total <= 0:
            return {}

    out: dict[str, float] = {}
    for e in eligible:
        # Weighted pick-one: the entry's share of the single guaranteed pick.
        # Independent roll: the entry's own drop probability.
        p = (e.get("proba", 0) / total) if weighted else e.get("proba", 0)
        if "item" in e:
            out[e["item"]] = out.get(e["item"], 0.0) + p
        elif "lootTable" in e:
            sub = predict(e["lootTable"], level, conds, _depth + 1, _visited)
            for k, v in sub.items():
                out[k] = out.get(k, 0.0) + p * v
    return out


def _entry_ok(e: dict, level: int, conds: int | None) -> bool:
    """Level- and conds-gate for a single loot entry (in-game default: no
    conds filter). `conds` bits required by the entry must all be set."""
    if "minLvl" in e and level < e["minLvl"]:
        return False
    if "maxLvl" in e and level > e["maxLvl"]:
        return False
    if conds is not None and "conds" in e and (e["conds"] & conds) != e["conds"]:
        return False
    return True


def predict_sorted(tid: str, level: int, conds: int | None = None
                   ) -> list[tuple[str, float, str, str]]:
    """Rows (item, prob, rarity, type) sorted by descending probability."""
    items = _items()
    rows = []
    for item, prob in predict(tid, level, conds).items():
        meta = items.get(item, {})
        rows.append((item, prob, meta.get("rarity", ""), meta.get("type", "")))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def table_ids() -> list[str]:
    return sorted(_tables().keys())


def table_exists(tid: str | None) -> bool:
    return bool(tid) and tid in _tables()
