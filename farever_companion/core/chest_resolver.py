"""Chest loot-table resolution + static/live merge (pure policy, no memory reads).

Pulled out of LiveModel: the per-chest table cache, the boss re-attribution
rules, and the merge of the static overworld chest index with the live scene's
chest elements are a self-contained policy. The model owns the live scene and
hands in the current dungeon boss + the live chest list; this class owns the
caching and the merge so LiveModel stays a thin coordinator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..data import units as udata
from ..geo import chests as chestdb

log = logging.getLogger(__name__)


@dataclass
class ChestRow:
    chest_id: str
    dist: float
    loot_table: str | None
    level: int | None
    state: str | None
    live: bool
    anomaly: bool = False


class ChestResolver:
    def __init__(self):
        try:
            self.chests = chestdb.load_chests()
        except Exception as e:
            log.warning("chest geo load failed (overworld POIs disabled): %s", e)
            self.chests = []
        self._static_ids = {c.chest_id for c in self.chests}
        self._table_cache: dict[str, str | None] = {}

    def chest_table(self, chest_id: str, dungeon_boss: str | None,
                    default_table: str | None = None) -> str | None:
        """Resolve a chest's loot table. Non-boss chests are cached once (so they
        can't flicker). Boss chests are NEVER cached — the static index maps the
        generic `BossChest` template to a placeholder (`UpgradeItems_Activity`),
        so we re-attribute it to the live dungeon boss every frame. Caching that
        would lock in the placeholder (if resolved before the boss loaded) or a
        stale boss carried over from a previous dungeon."""
        boss_chest = chest_id.startswith("BossChest")
        if not boss_chest and chest_id in self._table_cache:
            return self._table_cache[chest_id]
        tbl = default_table or chestdb.loot_table_for(chest_id)
        if boss_chest and dungeon_boss and (not tbl or not udata.is_boss(tbl)):
            tbl = udata.boss_loot_table(dungeon_boss) or tbl
        if tbl and not boss_chest:
            self._table_cache[chest_id] = tbl
        return tbl

    def nearest_chests_merged(self, xyz, n: int, dungeon_boss: str | None,
                              live_chests, max_dist: float = 0.0) -> list[ChestRow]:
        rows: dict[str, ChestRow] = {}
        for c, d in chestdb.nearest(self.chests, *xyz, n=10 ** 6):
            tbl = self.chest_table(c.chest_id, dungeon_boss, c.loot_table)
            # A static (overworld) boss chest for a DIFFERENT boss can't be in
            # the current boss instance — drop it so e.g. Crabgantua's arena
            # chest doesn't leak in while you're fighting Cleodora.
            if (tbl and dungeon_boss and tbl != dungeon_boss
                    and udata.is_boss(tbl)):
                continue
            rows[c.chest_id] = ChestRow(c.chest_id, d, tbl, c.level, None, False)
        for e in live_chests:
            d = e.dist(*xyz)
            rows[e.elem_id or "?"] = ChestRow(
                e.elem_id or "?", d, self.chest_table(e.elem_id or "?", dungeon_boss),
                None, e.state, True, anomaly=(e.elem_id not in self._static_ids))
        out = sorted(rows.values(), key=lambda r: r.dist)
        if max_dist > 0:
            out = [r for r in out if r.dist <= max_dist]
        return out[:n]
