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

import logging
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger(__name__)

from .proc import Proc, ProcError
from .hl import Hl
from .scene import Scene, Entity, Element
from .player import PlayerLocator
from .chest_resolver import ChestResolver, ChestRow
from .damage_source import DamageSourceManager
from . import attributes
from ..combat.dps import DpsMeter
from ..data import loot, units as udata, rarity as rarity_mod

XYZ = tuple[float, float, float]


@dataclass
class Nearest:
    kind: str               # 'enemy' | 'chest'
    label: str
    dist: float
    loot_table: str | None
    level: int
    note: str = ""


class LiveModel:
    UNITS_TTL = 0.10
    CHEST_DIST_BIAS = 0.5      # chests win over a slightly-closer enemy

    def __init__(self, proc: Proc):
        self.proc = proc
        self.hl = Hl(proc)
        self.scene = Scene(proc, self.hl)
        # Player locate is pure memory scan, zero writes to the game. There is no
        # code-injection path: the tool only ever reads the game's memory.
        self.locator = PlayerLocator(proc, self.hl)
        # Static+live chest loot-table resolution (cache + boss re-attribution).
        self.chests_resolver = ChestResolver()
        self._units_cache: list[Entity] = []
        self._units_at = 0.0
        self.dungeon_boss: str | None = None
        self.dps = DpsMeter()              # HP-diff (by-enemy) -> DPS meter panel
        # Per-skill DamageDisplay source (lifecycle + calibration + its own meter).
        # OPT-IN + experimental; HP-diff (`self.dps`) is the always-on fallback.
        self.damage = DamageSourceManager(proc)
        self._combat_at = 0.0
        # survivability: own-HP ring buffer + death edge detection
        self.player_hp_log: deque[tuple[float, float]] = deque(maxlen=240)
        self.player_max_hp: float = 0.0
        self.deaths: int = 0
        self._was_alive: bool = False

    # --- lifecycle -------------------------------------------------------
    def locate_player(self) -> int | None:
        addr = self.locator.locate()
        if addr:
            # warm up the (slow, ~1 min) DamageDisplay type-locate in the
            # background now, so per-skill DPS is ready by the time it's used.
            self.damage.warmup(addr)
        return addr

    # Per-skill (DamageDisplay) is owned by DamageSourceManager; these thin
    # delegators keep the Skills panel / overlays on their existing model API.
    @property
    def dps_events(self) -> DpsMeter:
        return self.damage.dps_events

    @property
    def per_skill_enabled(self) -> bool:
        return self.damage.per_skill_enabled

    def per_skill_status(self) -> str:
        return self.damage.status()

    def per_skill_progress(self) -> tuple[str, float]:
        return self.damage.progress()

    def recalibrate_skills(self) -> None:
        self.damage.recalibrate()

    def camera_yaw(self) -> float | None:
        """Free-look camera yaw — not available in the pure-read build (it would
        require a hook). Always None; the minimap rotates by player heading."""
        return None

    def shutdown(self) -> None:
        """Stop background threads. Pure-read tool: nothing to restore in-game."""
        self.damage.shutdown()

    @property
    def player_addr(self) -> int | None:
        # Re-resolve the live hero from the session-stable anchor each access so
        # every per-frame consumer (scene, dps, minimap, the attach watcher's
        # located/lost reconciliation) follows menu<->world + character changes.
        return self.locator.live_address()

    def player_xyz(self) -> XYZ | None:
        try:
            return self.locator.read_xyz()
        except ProcError:
            return None

    def player_heading(self) -> float | None:
        """Facing in radians (atan2: 0=+x, CCW), or None. Pure read of the
        heading field; see PlayerLocator.read_heading."""
        fn = getattr(self.locator, "read_heading", None)
        if fn is None:
            return None
        try:
            return fn()
        except ProcError:
            return None

    @property
    def chests(self):
        """Static overworld chest index (read by the minimap + status log)."""
        return self.chests_resolver.chests

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
        # Track the boss in the CURRENT instance, and CLEAR it when none is present
        # so a previous dungeon's boss can't linger. The old code never cleared it,
        # so e.g. Cleodora's difficulty showed while you were actually in a dungeon
        # whose boss isn't recognized. (The speedrun keeps its own sticky copy, so
        # clearing here doesn't disturb kill detection at the boss's despawn.)
        self.dungeon_boss = next(
            (e.unit_id for e in self._units_cache
             if e.unit_id and udata.is_boss(e.unit_id)), None)
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
        """'hard' | 'normal' inferred from live unit levels vs their CDB (normal)
        levels — Hard scales the dungeon up. None if levels can't be read, so
        callers fall back to the manual mode selector."""
        # Primary: a recognized dungeon boss — compare its live level to its CDB
        # normal level (Hard bumps the boss up).
        bid = self.dungeon_boss
        if bid:
            live = self.boss_level()
            info = udata.unit_info(bid)
            normal = info.get("lvl") if info else None
            if live is not None and isinstance(normal, int):
                return "hard" if live > normal else "normal"
            if live is not None:
                return "hard" if live >= self.HARD_LEVEL else "normal"
        # Fallback for dungeons whose boss isn't tagged in the data (e.g. the bee
        # dungeon, whose royals carry no boss flag/table): read the trash+elite
        # FLEET. Hard bumps every unit's level above its CDB normal, so several
        # nearby enemies reading live > CDB == Hard; all matching == Normal.
        return self._mode_from_fleet()

    def _mode_from_fleet(self) -> str | None:
        xyz = self.player_xyz()
        if not xyz:
            return None
        bumped = readable = 0
        for e, _d in self.nearest_enemies(xyz, 12, max_dist=0.0):
            if not e.unit_id:
                continue
            info = udata.unit_info(e.unit_id)
            normal = info.get("lvl") if info else None
            if not isinstance(normal, int):
                continue
            live = attributes.level(self.hl, e.addr)
            if live is None:
                continue
            readable += 1
            if live > normal:
                bumped += 1
        if readable < 3:
            return None                       # not enough signal to be confident
        # Hard scales every unit's level above its CDB normal, so a couple of clear
        # over-level reads positively identify Hard. "Not bumped" can't tell a
        # NORMAL dungeon apart from the open world (both sit at CDB level), so we
        # report None there and let the boss path / manual selector decide.
        return "hard" if bumped >= 2 else None

    # --- loot resolution (delegated to ChestResolver) --------------------
    def chest_table(self, chest_id: str, default_table: str | None = None) -> str | None:
        return self.chests_resolver.chest_table(chest_id, self.dungeon_boss,
                                                default_table)

    def nearest_chests_merged(self, xyz: XYZ, n: int, max_dist: float = 0.0
                              ) -> list[ChestRow]:
        return self.chests_resolver.nearest_chests_merged(
            xyz, n, self.dungeon_boss, self.live_chests(), max_dist)

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
        except Exception as e:
            log.warning("loot predict failed for table %r: %s", near.loot_table, e)
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
    def sample_combat(self, radius: float = 30.0) -> DpsMeter:
        """Sample BOTH combat sources each tick (cached for 0.2s):
          - HP-diff (by-enemy, team/area total) -> `self.dps`, the DPS-meter panel.
            Always on, so the headline DPS + by-enemy breakdown + survivability never
            depend on per-skill event capture (fixes 'idle' while in combat).
          - per-skill DamageDisplay events -> `self.dps_events`, the Skill panel.
            A SEPARATE meter so it never double-counts the HP-diff total.
        Returns `self.dps` (the DPS-meter source); the Skill panel reads
        `self.dps_events`."""
        now = time.monotonic()
        if now - self._combat_at < 0.1:        # re-sample at up to ~10 Hz (smooth UI)
            return self.dps
        self._combat_at = now

        # (1) HP-diff -> DPS meter. Bosses tracked regardless of radius.
        xyz = self.player_xyz()
        snap = []
        for e in self.units():
            if not e.is_enemy:
                continue
            boss = bool(e.unit_id and udata.is_boss(e.unit_id))
            if radius and xyz and not boss and e.dist(*xyz) > radius:
                continue
            snap.append((e.addr, e.unit_id or "?", attributes.health(self.hl, e.addr)))
        self.dps.update(snap)
        self._sample_player_hp(now)             # survivability (HP / taken) -> self.dps

        # (2) per-skill events -> Skill panel meter (owned by DamageSourceManager,
        # which drains its poller into `dps_events` and self-heals its GC cluster
        # using the HP-diff "in combat" signal).
        self.damage.sample(self.player_addr, self.dps.in_combat)
        return self.dps

    # --- survivability ---------------------------------------------------
    def _sample_player_hp(self, now: float) -> None:
        """Sample own HP into the ring buffer + count death edges (alive->0).
        Best-effort: silently no-ops if the player isn't located/readable."""
        pa = self.player_addr
        if not pa:
            return
        try:
            hp = attributes.health(self.hl, pa)
        except ProcError:
            return
        if hp is None:
            return
        self.player_max_hp = max(self.player_max_hp, hp)
        # Damage TAKEN = our own HP dropping. This is the reliable source for
        # DTPS / TAKEN / death recap (the survivability strip), so those numbers
        # always move WITH the HP bar. Guard against rebaseline/respawn (a drop
        # larger than max HP, or a heal/up-tick) so we never count phantom damage.
        if self.player_hp_log:
            prev = self.player_hp_log[-1][1]
            drop = prev - hp
            if 0 < drop < (self.player_max_hp or drop) * 1.5:
                self.dps.add_taken(drop, now)
        self.player_hp_log.append((now, hp))
        if self._was_alive and hp <= 0:
            self.deaths += 1
        self._was_alive = hp > 0

    def player_hp_series(self) -> list[float]:
        return [hp for _t, hp in self.player_hp_log]

    def player_hp_frac(self) -> float | None:
        """Current HP / max-seen, or None if no sample yet."""
        if not self.player_hp_log or self.player_max_hp <= 0:
            return None
        return max(0.0, min(1.0, self.player_hp_log[-1][1] / self.player_max_hp))

    def reset_combat(self) -> None:
        """Full combat reset: the meter plus the survivability buffers (so the
        overlay's Reset button clears HP history + the death count too)."""
        self.dps.reset()
        self.damage.reset()
        self.player_hp_log.clear()
        self.player_max_hp = 0.0
        self.deaths = 0
        self._was_alive = False
