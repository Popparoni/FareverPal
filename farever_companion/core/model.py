"""LiveModel — the single source of live truth, shared by every view.

Owns the process handle, the player locator (pure-read, no hook), the scene
reader and the DPS meter. Produces nearest-entity / loot / combat data on
demand; the overlays and the minimap all read from this one model so they're
always on the same frame and never double-read.

Bug fixes vs the old tool, enforced here:
  - BOSS DPS: `sample_combat` tracks every enemy within `radius` PLUS every boss
    regardless of distance (the old 30u cap dropped large/ranged bosses).
  - LOOT CONSISTENCY: a chest's loot table is resolved by the pure
    `geo.chests.loot_table_for` and then CACHED per chest id for the session, so
    the displayed table never flickers as a chest loads/unloads as a live entity.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .proc import Proc, ProcError
from .hl import Hl
from .scene import Scene, Entity, Element
from .player import PlayerLocator, HookLocator, CameraLocator
from . import attributes
from ..combat.dps import DpsMeter
from ..data import loot, units as udata, rarity as rarity_mod
from ..geo import chests as chestdb

XYZ = tuple[float, float, float]


@dataclass
class Nearest:
    kind: str               # 'enemy' | 'chest'
    label: str
    dist: float
    loot_table: str | None
    level: int
    note: str = ""


@dataclass
class ChestRow:
    chest_id: str
    dist: float
    loot_table: str | None
    level: int | None
    state: str | None
    live: bool
    anomaly: bool = False


class LiveModel:
    UNITS_TTL = 0.10
    CHEST_DIST_BIAS = 0.5      # chests win over a slightly-closer enemy

    def __init__(self, proc: Proc):
        self.proc = proc
        self.hl = Hl(proc)
        self.scene = Scene(proc, self.hl)
        # Fast read-only-effect code hook (the old tool / CT method). The pure-
        # read scan PlayerLocator remains available as a no-write fallback.
        self.locator = HookLocator(proc, self.hl)
        self.camera = CameraLocator(proc)     # free-look yaw (best-effort 2nd hook)
        try:
            self.chests = chestdb.load_chests()
        except Exception:
            self.chests = []
        self._static_ids = {c.chest_id for c in self.chests}
        self._table_cache: dict[str, str | None] = {}
        self._units_cache: list[Entity] = []
        self._units_at = 0.0
        self.dungeon_boss: str | None = None
        self.dps = DpsMeter()
        self._combat_at = 0.0
        self._damage = None        # lazily created DamageReader (Phase 6)

    # --- lifecycle -------------------------------------------------------
    def locate_player(self) -> int | None:
        addr = self.locator.locate()
        if addr:
            try:
                self.camera.enable()      # best-effort; minimap falls back if absent
            except Exception:
                pass
        return addr

    def camera_yaw(self) -> float | None:
        """Free-look camera yaw in radians (-pi..pi), or None if the camera hook
        isn't active. Updates as the mouse orbits."""
        try:
            return self.camera.yaw()
        except Exception:
            return None

    def shutdown(self) -> None:
        """Restore both hooks (if installed). Call before closing proc."""
        for h in (self.locator, self.camera):
            disable = getattr(h, "disable", None)
            if callable(disable):
                try:
                    disable()
                except Exception:
                    pass

    @property
    def player_addr(self) -> int | None:
        return self.locator.address

    def player_xyz(self) -> XYZ | None:
        try:
            return self.locator.read_xyz()
        except ProcError:
            return None

    def player_heading(self) -> float | None:
        """Facing in radians (atan2: 0=+x, CCW), or None. See HookLocator."""
        fn = getattr(self.locator, "read_heading", None)
        if fn is None:
            return None
        try:
            return fn()
        except ProcError:
            return None

    # --- scene -----------------------------------------------------------
    def units(self) -> list[Entity]:
        now = time.monotonic()
        if now - self._units_at < self.UNITS_TTL and self._units_cache:
            return self._units_cache
        try:
            self._units_cache = self.scene.units(self.player_addr)
        except ProcError:
            self._units_cache = []
        self._units_at = now
        for e in self._units_cache:
            if e.unit_id and udata.is_boss(e.unit_id):
                self.dungeon_boss = e.unit_id
                break
        return self._units_cache

    def enemies(self) -> list[Entity]:
        return [e for e in self.units() if e.is_enemy]

    def nearest_enemies(self, xyz: XYZ, n: int, max_dist: float = 0.0,
                        enemies_only: bool = True):
        pool = [e for e in self.units()
                if (e.is_enemy if enemies_only else (e.is_foe or e.is_hero))]
        ranked = sorted(((e, e.dist(*xyz)) for e in pool), key=lambda t: t[1])
        if max_dist > 0:
            ranked = [(e, d) for e, d in ranked if d <= max_dist]
        return ranked[:n]

    def live_chests(self) -> list[Element]:
        try:
            return [e for e in self.scene.elements(self.player_addr) if e.is_chest]
        except ProcError:
            return []

    def gatherables(self) -> list[Element]:
        try:
            return [e for e in self.scene.elements(self.player_addr) if e.is_gatherable]
        except ProcError:
            return []

    def obelisks(self) -> list[Element]:
        try:
            return [e for e in self.scene.elements(self.player_addr) if e.is_obelisk]
        except ProcError:
            return []

    def boss_state(self) -> tuple[str | None, bool, float | None]:
        """(boss_id, present, hp) for the current dungeon boss. `present` is
        whether the boss entity is in the live scene; `hp` its current health
        (f64) or None if unreadable. Used by the speedrun auto-stop."""
        scene = self.units()        # MUST run first: it's what populates dungeon_boss
        bid = self.dungeon_boss
        if not bid:
            return (None, False, None)
        for e in scene:
            if e.unit_id == bid:
                try:
                    return (bid, True, attributes.health(self.hl, e.addr))
                except ProcError:
                    return (bid, True, None)
        return (bid, False, None)   # boss known (sticky) but not in scene now

    HARD_LEVEL = 20     # Hard mode scales every dungeon to this level

    def boss_level(self) -> int | None:
        """Live level of the current dungeon boss, or None (offset uncalibrated or
        boss not in scene). See core/attributes.level."""
        bid = self.dungeon_boss
        if not bid:
            return None
        for e in self.units():
            if e.unit_id == bid:
                try:
                    return attributes.level(self.hl, e.addr)
                except ProcError:
                    return None
        return None

    def detected_mode(self) -> str | None:
        """'hard' | 'normal' inferred from the live boss level vs its normal
        (CDB) level — Hard scales the dungeon up. None if the live level can't be
        read, so callers fall back to the manual mode selector."""
        bid = self.dungeon_boss
        if not bid:
            return None
        live = self.boss_level()
        if live is None:
            return None
        info = udata.unit_info(bid)
        normal = info.get("lvl") if info else None
        if isinstance(normal, int):
            return "hard" if live > normal else "normal"
        return "hard" if live >= self.HARD_LEVEL else "normal"

    # --- loot resolution (deterministic + cached) ------------------------
    def chest_table(self, chest_id: str, default_table: str | None = None) -> str | None:
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
        if boss_chest and self.dungeon_boss and (not tbl or not udata.is_boss(tbl)):
            tbl = udata.boss_loot_table(self.dungeon_boss) or tbl
        if tbl and not boss_chest:
            self._table_cache[chest_id] = tbl
        return tbl

    def nearest_chests_merged(self, xyz: XYZ, n: int, max_dist: float = 0.0
                              ) -> list[ChestRow]:
        rows: dict[str, ChestRow] = {}
        for c, d in chestdb.nearest(self.chests, *xyz, n=10 ** 6):
            tbl = self.chest_table(c.chest_id, c.loot_table)
            # A static (overworld) boss chest for a DIFFERENT boss can't be in
            # the current boss instance — drop it so e.g. Crabgantua's arena
            # chest doesn't leak in while you're fighting Cleodora.
            if (tbl and self.dungeon_boss and tbl != self.dungeon_boss
                    and udata.is_boss(tbl)):
                continue
            rows[c.chest_id] = ChestRow(c.chest_id, d, tbl, c.level, None, False)
        for e in self.live_chests():
            d = e.dist(*xyz)
            rows[e.elem_id or "?"] = ChestRow(
                e.elem_id or "?", d, self.chest_table(e.elem_id or "?"), None,
                e.state, True, anomaly=(e.elem_id not in self._static_ids))
        out = sorted(rows.values(), key=lambda r: r.dist)
        if max_dist > 0:
            out = [r for r in out if r.dist <= max_dist]
        return out[:n]

    def closest_loot(self, xyz: XYZ, default_level: int) -> Nearest | None:
        cands: list[tuple[float, Nearest]] = []
        for e, d in self.nearest_enemies(xyz, 6, 0.0, enemies_only=True):
            if not e.unit_id:
                continue
            tbl = udata.loot_table_for_unit(e.unit_id)
            if not tbl:
                continue
            info = udata.unit_info(e.unit_id)
            lvl = (info.get("lvl") if info else None) or default_level
            cands.append((d, Nearest("enemy", e.unit_id, d, tbl, lvl,
                                     note=(info.get("type") if info else "") or "")))
            break
        for cr in self.nearest_chests_merged(xyz, n=15):
            tbl = cr.loot_table
            if not tbl:
                continue
            cands.append((cr.dist * self.CHEST_DIST_BIAS,
                          Nearest("chest", cr.chest_id, cr.dist, tbl,
                                  cr.level or default_level, note=cr.state or "")))
            break
        if not cands:
            return None
        return min(cands, key=lambda t: t[0])[1]

    def enemy_drop_source(self, entity, dist: float, default_level: int) -> Nearest | None:
        """Build a Nearest for a SPECIFIC enemy so its drop table can be shown
        (the entity overlay's selected-target view)."""
        uid = getattr(entity, "unit_id", None)
        if not uid:
            return None
        tbl = udata.loot_table_for_unit(uid)
        if not tbl:
            return None
        info = udata.unit_info(uid)
        lvl = (info.get("lvl") if info else None) or default_level
        return Nearest("enemy", uid, dist, tbl, lvl,
                       note=(info.get("type") if info else "") or "")

    def chest_drop_source(self, chestrow, default_level: int) -> Nearest | None:
        """Build a Nearest for a chest row so its drop table can be shown."""
        if not getattr(chestrow, "loot_table", None):
            return None
        return Nearest("chest", chestrow.chest_id, chestrow.dist, chestrow.loot_table,
                       chestrow.level or default_level, note=chestrow.state or "")

    def drop_table(self, near: Nearest | None):
        if not near or not near.loot_table:
            return []
        try:
            return loot.predict_sorted(near.loot_table, near.level)
        except Exception:
            return []

    def drop_table_effective(self, near: Nearest | None):
        """Confirmed drops + rarity rolls; procedural generators left opaque."""
        out = []
        for item, prob, rar, typ in self.drop_table(near):
            if rarity_mod.should_promote(item, typ, rar):
                for tier, ch in rarity_mod.promote_distribution(rar, near.level).items():
                    if ch > 0:
                        out.append((item, prob * ch, tier, "rolled"))
            else:
                out.append((item, prob, rar, typ))
        return out

    # --- combat ----------------------------------------------------------
    def _damage_source(self):
        """The DamageDisplay event reader, once it's calibrated (Phase 6).
        Returns None until then so we use the HP-diff fallback."""
        from .damage import DamageReader
        if not DamageReader.calibrated() or not self.proc.has_scan:
            return None
        if self._damage is None:
            self._damage = DamageReader(self.proc, self.hl, self.player_addr)
        return self._damage

    def sample_combat(self, radius: float = 30.0) -> DpsMeter:
        now = time.monotonic()
        if now - self._combat_at < 0.2:
            return self.dps
        self._combat_at = now

        # preferred: per-skill events from ui.comp.DamageDisplay (write-free)
        src = self._damage_source()
        if src is not None:
            try:
                for ev in src.poll():
                    self.dps.add_event(ev)
                return self.dps
            except ProcError:
                pass   # fall through to HP-diff on a transient read error

        # fallback: HP-diff (team/area total). Bosses always tracked.
        xyz = self.player_xyz()
        snap = []
        for e in self.units():
            if not e.is_enemy:
                continue
            boss = bool(e.unit_id and udata.is_boss(e.unit_id))
            # FIX: bosses are tracked regardless of distance; others gated by radius
            if radius and xyz and not boss and e.dist(*xyz) > radius:
                continue
            snap.append((e.addr, e.unit_id or "?", attributes.health(self.hl, e.addr)))
        self.dps.update(snap)
        return self.dps
