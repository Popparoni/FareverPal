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
from collections import deque
from dataclasses import dataclass

from .proc import Proc, ProcError
from .hl import Hl
from .scene import Scene, Entity, Element
from .player import PlayerLocator
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
        # Player locate is pure memory scan, zero writes to the game. There is no
        # code-injection path: the tool only ever reads the game's memory.
        self.locator = PlayerLocator(proc, self.hl)
        try:
            self.chests = chestdb.load_chests()
        except Exception:
            self.chests = []
        self._static_ids = {c.chest_id for c in self.chests}
        self._table_cache: dict[str, str | None] = {}
        self._units_cache: list[Entity] = []
        self._units_at = 0.0
        self.dungeon_boss: str | None = None
        self.dps = DpsMeter()              # HP-diff (by-enemy) -> DPS meter panel
        self.dps_events = DpsMeter()       # per-skill DamageDisplay events -> Skill panel
        self._combat_at = 0.0
        import threading as _th
        self._redrive = _th.Event()        # combat-triggered cluster re-derive request
        self._damage = None        # lazily created DamageReader (Phase 6)
        self._damage_scan_started = False   # the type-locate runs once, off-thread
        self._damage_scan_t0 = 0.0          # when calibration started (for progress UI)
        self._combat_q: deque = deque(maxlen=20000)   # bg poller -> UI drain
        self._poller_started = False
        self._stop = False         # signals background threads to exit
        # Per-skill (DamageDisplay) is OPT-IN (config `dps_per_skill`, default OFF
        # until validated live). The old "can't be live" blocker is solved by the
        # cluster scan: HashLink's GC is non-moving + size-class segregated, so the
        # transient display objects cluster in a few GC pages we can re-scan in
        # sub-100ms each tick (see DPS_METER_PLAN). HP-diff stays the fallback.
        # Per-skill is gated behind the experimental flag (release builds hide it —
        # incomplete coverage + slow calibration; see config.experimental_enabled).
        # Even if dps_per_skill is True in settings, it stays off without the flag.
        try:
            from ..config import Settings, experimental_enabled
            self.per_skill_enabled = bool(experimental_enabled()
                                          and Settings.load().dps_per_skill)
        except Exception:
            self.per_skill_enabled = False
        # survivability: own-HP ring buffer + death edge detection
        self.player_hp_log: deque[tuple[float, float]] = deque(maxlen=240)
        self.player_max_hp: float = 0.0
        self.deaths: int = 0
        self._was_alive: bool = False

    # --- lifecycle -------------------------------------------------------
    def locate_player(self) -> int | None:
        addr = self.locator.locate()
        if addr:
            # kick off the (slow, ~1 min) DamageDisplay type-locate now, in the
            # background, so per-skill DPS is ready by the time the meter is used.
            try:
                self._damage_source()
            except Exception:
                pass
        return addr

    def per_skill_status(self) -> str:
        """'active' | 'scanning' | 'off' — drives the meter's 'calibrating' hint.
        'off' = uncalibrated or no scan backend; 'scanning' = the type-locate /
        cluster mapping is running; 'active' = the cluster ranges are ready."""
        from .damage import DamageReader
        if not DamageReader.calibrated() or not self.proc.has_scan:
            return "off"
        if self._damage is not None and getattr(self._damage, "_ranges_ready", False):
            return "active"
        return "scanning" if self._damage_scan_started else "off"

    def per_skill_progress(self) -> tuple[str, float]:
        """Calibration progress for the Skills panel: (phase_label, fraction 0..1).
        Returns ('', 1.0) once ready and ('', 0.0) when per-skill is off. The
        fractions are elapsed-time estimates (the Rust scans give no real %), so
        the bar advances steadily and caps just shy of full until each phase ends.
        Phase 1 = locate the DamageDisplay type (~full-heap name scan, the slow
        one); phase 2 = map the GC cluster ranges (a heap sweep for instances)."""
        from .damage import DamageReader
        if (not self.per_skill_enabled or not DamageReader.calibrated()
                or not self.proc.has_scan):
            return ("", 0.0)
        d = self._damage
        if d is None or not self._damage_scan_started:
            return ("starting…", 0.02)
        if getattr(d, "_ranges_ready", False):
            return ("", 1.0)
        el = time.monotonic() - (self._damage_scan_t0 or time.monotonic())
        if d._type_ptr is None:
            return (f"locating damage type · {el:.0f}s", min(0.60, 0.05 + el / 150.0))
        return (f"mapping — keep attacking · {el:.0f}s", min(0.96, 0.62 + el / 100.0))

    def recalibrate_skills(self) -> None:
        """User-triggered: re-map the per-skill cluster NOW (attack while it runs).
        Wakes the maintenance thread to re-derive the GC ranges against the live
        DamageDisplay instances on screen — so a cluster that mapped during a lull
        (no numbers) is fixed without waiting for the auto self-heal. No-op until
        per-skill is enabled + the type is located."""
        self._start_damage_type_scan()      # ensure the maintenance thread exists
        self._redrive.set()

    def camera_yaw(self) -> float | None:
        """Free-look camera yaw — not available in the pure-read build (it would
        require a hook). Always None; the minimap rotates by player heading."""
        return None

    def shutdown(self) -> None:
        """Stop background threads. Pure-read tool: nothing to restore in-game."""
        self._stop = True          # let the background poller / scan threads exit

    @property
    def player_addr(self) -> int | None:
        return self.locator.address

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
        """The per-skill DamageDisplay reader — OPT-IN only (`per_skill_enabled`).
        The display objects are transient (~1s) with no shared parent container, so
        we can't read one container's children. Instead we exploit HashLink's
        non-moving, size-class-segregated GC: every live DamageDisplay clusters in
        the same handful of GC pages, so one slow full scan locates the cluster and
        every tick re-scans ONLY those ranges (sub-100ms) — fast enough to catch a
        number mid-life. Default OFF until validated live. Returns the reader once
        its cluster ranges are derived (else None -> HP-diff in the meantime)."""
        if not self.per_skill_enabled:
            return None
        from .damage import DamageReader
        if not DamageReader.calibrated() or not self.proc.has_scan:
            return None
        if self._damage is None:
            # dedicated Hl so the background poller's reflection caches don't race
            # with the UI thread's self.hl.
            self._damage = DamageReader(self.proc, Hl(self.proc), self.player_addr)
        self._damage.my_hero = self.player_addr     # for incoming-damage tagging
        self._start_damage_type_scan()
        return self._damage if getattr(self._damage, "_ranges_ready", False) else None

    def _start_damage_type_scan(self) -> None:
        # ONE maintenance thread (the flag is never cleared): locate the type, then
        # periodically re-derive the cluster ranges (a slow full heap sweep) so they
        # track the GC as it grows new size-class pages. The fast per-tick range
        # scans happen in the poller. Only runs while per-skill is enabled (exp).
        if self._damage_scan_started:
            return
        self._damage_scan_started = True
        self._damage_scan_t0 = time.monotonic()

        def _maintain():
            while not self._stop and self.per_skill_enabled:
                n = 0
                try:
                    n = self._damage.refresh_ranges()  # full sweep; derives _scan_ranges
                except Exception:
                    pass
                self._redrive.clear()
                # Adaptive spacing. A derive during a LULL finds only the persistent
                # ~handful of instances, so the cluster may miss size-class pages that
                # only fill during combat -> re-derive soon. A healthy combat-time
                # derive (many instances) anchors those pages -> relax to ~5 min. AND
                # wake immediately if combat signals the cluster caught nothing
                # (`_redrive`), so a lull-derived cluster self-heals the moment you fight.
                interval = 45 if n < 16 else 300
                waited = 0.0
                while waited < interval:
                    if self._stop or not self.per_skill_enabled:
                        return
                    if self._redrive.wait(timeout=1.0):
                        break                  # combat asked for a fresh derive
                    waited += 1.0
        import threading
        threading.Thread(target=_maintain, daemon=True, name="dmg-maintain").start()

    def _ensure_combat_poller(self, src) -> None:
        """Run `src.poll()` on a BACKGROUND thread, feeding events into a queue
        the UI thread drains. poll() is now a RANGE-BOUNDED scan (only the GC
        size-class pages the cluster occupies, ~tens of MB -> sub-100ms), not a
        full-heap sweep — but it still lives off the UI thread so a slow read can
        never stall a frame, and so it can poll fast enough to catch ~1s numbers."""
        if self._poller_started:
            return
        self._poller_started = True

        import sys

        def _log(m):
            print(f"[dmg-poller] {m}", file=sys.stderr, flush=True)

        def _loop():
            _log(f"started; type_ptr={getattr(src, '_type_ptr', None)}")
            last_ev = 0.0
            total = 0
            polls = 0
            last_report = time.monotonic()
            while not self._stop:
                t0 = time.monotonic()
                try:
                    evs = src.poll()
                except (ProcError, OSError) as e:
                    _log(f"poll read error: {e}")
                    evs = []
                except Exception as e:
                    _log(f"poll EXC: {type(e).__name__}: {e}")
                    evs = []
                dt = time.monotonic() - t0
                for ev in evs:
                    self._combat_q.append(ev)
                total += len(evs)
                polls += 1
                now2 = time.monotonic()
                if now2 - last_report > 2.0:
                    _log(f"polls={polls} last={len(evs)}ev/{dt:.1f}s total={total} q={len(self._combat_q)}")
                    last_report = now2
                # Poll FAST throughout a fight: a number lives ~1s, so 0.08s polling
                # catches each one ~12x (never missed) as long as the cluster covers
                # it. The OLD idle ramp-up to 2.5s was the stutter cause — it slept
                # through the start of the next burst, dropping skills. poll() is
                # cheap now (range-bounded), so stay tight while events flowed within
                # the last 3s, and only back off after a real lull (saves CPU).
                if evs:
                    last_ev = now2
                in_combat = (now2 - last_ev) < 3.0
                time.sleep(0.08 if in_combat else 0.5)
        import threading
        threading.Thread(target=_loop, daemon=True, name="dmg-poller").start()

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

        # (2) per-skill events -> Skill panel meter (if the source is calibrated).
        src = self._damage_source()
        if src is not None:
            self._ensure_combat_poller(src)
            drained = 0
            while self._combat_q and drained < 10000:
                self.dps_events.add_event(self._combat_q.popleft())
                drained += 1
            # Self-heal: clearly in combat (HP-diff sees damage) yet the event source
            # caught nothing -> the cluster was mapped during a lull and misses the
            # live-number GC pages. Ask the maintenance thread to re-derive NOW; a
            # combat-time derive anchors the right pages.
            if self.dps.in_combat and not self.dps_events.in_combat:
                self._redrive.set()
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
        self.dps_events.reset()
        self.player_hp_log.clear()
        self.player_max_hp = 0.0
        self.deaths = 0
        self._was_alive = False
