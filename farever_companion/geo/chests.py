"""Static chest index: id -> world position + loot table.

Reuses the already-built `notes/chest_positions.json` (229 chests resolved from
prefabs by the old tool) for positions, and `notes/chest_loot_index.json` for
loot-table resolution of dynamic/instance chests not in the position cache.

Coordinates are global world XYZ, 1:1 with the runtime player struct (verified
live in the old project). Pure data, no attached process.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from .. import paths
from ..data import cdb


@dataclass(frozen=True)
class Chest:
    chest_id: str
    loot_table: str | None
    x: float
    y: float
    z: float
    faction: str | None = None
    level: int | None = None
    name: str | None = None

    def dist(self, x: float, y: float, z: float) -> float:
        return math.dist((self.x, self.y, self.z), (x, y, z))


@lru_cache(maxsize=1)
def load_chests() -> list[Chest]:
    """All statically-resolved chests with positions."""
    try:
        payload = json.loads(paths.chest_positions_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for c in payload.get("chests", []):
        out.append(Chest(
            chest_id=c["chest_id"], loot_table=c.get("loot_table"),
            x=c["x"], y=c["y"], z=c["z"],
            faction=c.get("faction"), level=c.get("level"), name=c.get("name"),
        ))
    return out


@lru_cache(maxsize=1)
def _index() -> dict[str, str]:
    """{chest_id -> lootTable} from the raw index (incl. template rows)."""
    try:
        idx = json.loads(paths.chest_index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {r["chest_id"]: r["lootTable"] for r in idx.get("records", []) if r.get("lootTable")}


@lru_cache(maxsize=1)
def _table_ids() -> frozenset[str]:
    return frozenset(r["id"] for r in cdb.lines("lootTable"))


@lru_cache(maxsize=4096)
def loot_table_for(chest_id: str | None) -> str | None:
    """Resolve a chest id to its loot table, deterministically:
    1. exact match in the static index;
    2. the id IS itself a loot-table id (world-activity / crate chests);
    3. strip trailing _<n> instance suffixes and retry 1+2;
    4. longest template-prefix match in the index.
    Pure + cached, so a given id always resolves identically (no flicker)."""
    if not chest_id:
        return None
    idx = _index()
    tables = _table_ids()
    if chest_id in idx:
        return idx[chest_id]
    if chest_id in tables:
        return chest_id
    base = chest_id
    while True:
        stripped = re.sub(r"_\d+$", "", base)
        if stripped == base:
            break
        base = stripped
        if base in idx:
            return idx[base]
        if base in tables:
            return base
    cands = [k for k in idx if chest_id.startswith(k)]
    if cands:
        return idx[max(cands, key=len)]
    return None


def nearest(chests, x: float, y: float, z: float, n: int = 10):
    ranked = sorted(((c, c.dist(x, y, z)) for c in chests), key=lambda t: t[1])
    return ranked[:n]
