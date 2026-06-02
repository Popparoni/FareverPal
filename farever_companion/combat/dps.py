"""DPS engine — pure logic, headless-testable (no process reads here).

Two damage sources feed the same aggregator:

1. HP-DIFF (fallback): fed a per-tick snapshot of (addr, unit_id, hp), it diffs
   each enemy's Health over time to derive outgoing damage. Team/area total
   (can't attribute a hit to a player without a damage source). Bosses are
   tracked the same as any enemy — the caller must NOT drop them by radius
   (that was the old "boss HP missing" bug). Address reuse is guarded by a
   unit-id change / implausible HP jump (rebaseline, not huge damage). An enemy
   that vanishes while nearly dead and recently damaged is credited a kill.

2. EVENTS (preferred, per-skill): `add_event(DamageEvent)` from the
   `ui.comp.DamageDisplay` reader (see core/damage.py). Gives per-skill totals,
   crit%, max hit, kills — the game pre-filters to your own outgoing damage. An
   event carries a `kind` (damage|heal|shield) and an `incoming` flag (damage
   *taken* by self, for the survivability / DTPS readout). Per-skill stats are
   keyed by (kind, skill) so heals/shields never pollute the damage table.

Both maintain a rolling-window DPS/HPS/SPS, encounter totals, and per-target /
per-(kind,skill) breakdowns. The UI shows per-skill when events are present,
else per-target. The headline `current_dps()` is damage-only by design.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

KINDS = ("damage", "heal", "shield")


@dataclass
class Track:
    unit_id: str
    last_hp: float
    last_seen: float
    max_hp: float
    damaged_at: float = 0.0


@dataclass
class DamageEvent:
    amount: float
    skill: str = "?"
    crit: bool = False
    kill: bool = False
    target: str = "?"
    t: float = 0.0
    kind: str = "damage"        # damage | heal | shield
    incoming: bool = False      # True = damage TAKEN by self (DTPS / death recap)


@dataclass
class SkillStat:
    total: float = 0.0
    hits: int = 0
    crits: int = 0
    kills: int = 0
    max_hit: float = 0.0
    min_hit: float = 0.0        # 0.0 until the first hit lands

    # derived (computed, never stored) ------------------------------------
    @property
    def avg_hit(self) -> float:
        return self.total / self.hits if self.hits else 0.0

    @property
    def crit_pct(self) -> float:
        return self.crits / self.hits if self.hits else 0.0


class DpsMeter:
    WINDOW = 5.0          # rolling DPS window (s)
    IDLE_END = 7.0        # encounter ends after this long with no damage
    KILL_GRACE = 2.0      # despawn within this of last damage = killing blow
    LOG_CAP = 200_000     # safety bound on the per-encounter damage log

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.tracks: dict[int, Track] = {}
        # outgoing rolling window — (t, kind, amount)
        self.events: deque[tuple[float, str, float]] = deque()
        # incoming (damage taken) rolling window — (t, amount)
        self.incoming: deque[tuple[float, float]] = deque()
        self.per_target: dict[str, float] = {}
        self.per_skill: dict[tuple[str, str], SkillStat] = {}   # (kind, skill) -> stat
        self.recent_incoming: deque[DamageEvent] = deque(maxlen=24)
        self.recent_hits: deque[DamageEvent] = deque(maxlen=40)   # outgoing, for the live feed
        self.dmg_log: list[tuple[float, float]] = []            # (t, dmg) this encounter
        self.encounter_start: float | None = None
        self.encounter_end: float | None = None
        self.total: float = 0.0          # outgoing DAMAGE total (heal/shield kept apart)
        self.heal_total: float = 0.0
        self.shield_total: float = 0.0
        self.taken_total: float = 0.0
        self.peak: float = 0.0
        self.kills: int = 0
        self.has_events: bool = False    # True once a real DamageEvent arrives

    @property
    def damage_total(self) -> float:     # readable alias (mirrors heal_total/shield_total)
        return self.total

    # --- HP-diff source --------------------------------------------------
    def update(self, snapshot, now: float | None = None) -> None:
        """snapshot: iterable of (addr:int, unit_id:str, hp:float|None)."""
        now = time.monotonic() if now is None else now
        self._maybe_roll_encounter(now)

        seen = set()
        for addr, uid, hp in snapshot:
            if hp is None:
                continue
            seen.add(addr)
            tr = self.tracks.get(addr)
            reuse = tr is not None and (tr.unit_id != uid
                                        or (tr.max_hp and hp > tr.max_hp * 1.5))
            if tr is None or reuse:
                self.tracks[addr] = Track(uid, hp, now, max_hp=hp)
                continue
            tr.max_hp = max(tr.max_hp, hp)
            if hp < tr.last_hp:
                self._add_damage(now, tr.last_hp - hp, uid)
                tr.damaged_at = now
            tr.last_hp = hp
            tr.last_seen = now

        for addr in list(self.tracks):
            if addr in seen:
                continue
            tr = self.tracks[addr]
            near_dead = bool(tr.max_hp) and tr.last_hp <= 0.15 * tr.max_hp
            if tr.last_hp > 0 and near_dead and now - tr.damaged_at <= self.KILL_GRACE:
                self._add_damage(now, tr.last_hp, tr.unit_id)
                self.kills += 1
            del self.tracks[addr]

        self._prune(now)
        self.peak = max(self.peak, self.current_dps(now))

    # --- event source ----------------------------------------------------
    def add_event(self, ev: DamageEvent, now: float | None = None) -> None:
        now = ev.t or (time.monotonic() if now is None else now)
        self.has_events = True
        self._maybe_roll_encounter(now)
        if ev.amount <= 0:
            return
        if ev.incoming:
            self._add_incoming(now, ev)
            return
        self._add_damage(now, ev.amount, ev.target, kind=ev.kind)
        if not ev.t:
            ev.t = now
        self.recent_hits.append(ev)             # newest-last; the live feed reads the tail
        key = (ev.kind, ev.skill)
        st = self.per_skill.setdefault(key, SkillStat())
        st.min_hit = ev.amount if st.hits == 0 else min(st.min_hit, ev.amount)
        st.total += ev.amount
        st.hits += 1
        st.crits += 1 if ev.crit else 0
        st.kills += 1 if ev.kill else 0
        st.max_hit = max(st.max_hit, ev.amount)
        if ev.kill:
            self.kills += 1
        self.peak = max(self.peak, self.current_dps(now))

    def add_taken(self, amount: float, now: float | None = None) -> None:
        """Record damage TAKEN by self (for DTPS / TAKEN / death recap). Fed from
        the player's own HP drops (model `_sample_player_hp`) so the survivability
        numbers always track the HP bar — independent of whether the game renders a
        DamageDisplay for incoming damage. Does NOT set `has_events` (it's not a
        per-skill outgoing event), so it never flips the table into per-skill mode."""
        now = time.monotonic() if now is None else now
        if amount <= 0:
            return
        self._maybe_roll_encounter(now)
        self._add_incoming(now, DamageEvent(amount=amount, incoming=True, t=now))

    # --- shared ----------------------------------------------------------
    def _maybe_roll_encounter(self, now: float) -> None:
        if ((self.events or self.incoming) and self.encounter_end is not None
                and now - self.encounter_end > self.IDLE_END):
            self._reset_encounter()

    def _touch_encounter(self, now: float) -> None:
        # min/max so an out-of-order event (sources can interleave) never shrinks
        # the encounter span.
        self.encounter_start = (now if self.encounter_start is None
                                else min(self.encounter_start, now))
        self.encounter_end = (now if self.encounter_end is None
                              else max(self.encounter_end, now))

    def _add_damage(self, now: float, dmg: float, key: str, kind: str = "damage") -> None:
        if dmg <= 0:
            return
        self.events.append((now, kind, dmg))
        if kind == "heal":
            self.heal_total += dmg
        elif kind == "shield":
            self.shield_total += dmg
        else:                                   # damage
            self.total += dmg
            self.per_target[key] = self.per_target.get(key, 0.0) + dmg
            self.dmg_log.append((now, dmg))
            if len(self.dmg_log) > self.LOG_CAP:
                del self.dmg_log[: self.LOG_CAP // 2]
        self._touch_encounter(now)

    def _add_incoming(self, now: float, ev: DamageEvent) -> None:
        self.incoming.append((now, ev.amount))
        self.taken_total += ev.amount
        self.recent_incoming.append(ev)
        self._touch_encounter(now)

    def _prune(self, now: float) -> None:
        cutoff = now - self.WINDOW
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        while self.incoming and self.incoming[0][0] < cutoff:
            self.incoming.popleft()

    def _reset_encounter(self) -> None:
        self.events.clear()
        self.incoming.clear()
        self.per_target.clear()
        self.per_skill.clear()
        self.recent_incoming.clear()
        self.recent_hits.clear()
        self.dmg_log.clear()
        self.encounter_start = self.encounter_end = None
        self.total = 0.0
        self.heal_total = 0.0
        self.shield_total = 0.0
        self.taken_total = 0.0
        self.peak = 0.0
        self.kills = 0

    # --- read: rolling rates --------------------------------------------
    def _windowed(self, now: float | None, kind: str | None) -> float:
        now = time.monotonic() if now is None else now
        cutoff = now - self.WINDOW
        return sum(a for t, k, a in self.events
                   if t >= cutoff and (kind is None or k == kind)) / self.WINDOW

    def current_dps(self, now: float | None = None) -> float:
        return self._windowed(now, "damage")        # headline is damage-only

    def hps(self, now: float | None = None) -> float:
        return self._windowed(now, "heal")

    def sps(self, now: float | None = None) -> float:
        return self._windowed(now, "shield")

    def dtps(self, now: float | None = None) -> float:
        """Damage-taken per second (incoming), rolling window."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.WINDOW
        return sum(a for t, a in self.incoming if t >= cutoff) / self.WINDOW

    # --- read: encounter totals -----------------------------------------
    @property
    def duration(self) -> float:
        if self.encounter_start is None:
            return 0.0
        return (self.encounter_end or self.encounter_start) - self.encounter_start

    @property
    def encounter_dps(self) -> float:
        d = self.duration
        return self.total / d if d > 0 else 0.0

    def kind_total(self, kind: str) -> float:
        return {"heal": self.heal_total, "shield": self.shield_total}.get(kind, self.total)

    @property
    def in_combat(self) -> bool:
        return bool(self.events or self.incoming)

    # --- read: breakdowns -----------------------------------------------
    def top_targets(self, n: int = 6):
        return sorted(self.per_target.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def per_skill_of(self, kind: str) -> dict[str, SkillStat]:
        return {sk: st for (k, sk), st in self.per_skill.items() if k == kind}

    def has_kind(self, kind: str) -> bool:
        return any(k == kind for (k, _sk) in self.per_skill)

    def top_skills(self, n: int = 8, kind: str = "damage"):
        """(skill, SkillStat) rows for one kind, biggest total first. `n` is the
        first arg so the legacy positional `top_skills(N)` keeps working."""
        items = self.per_skill_of(kind).items()
        return sorted(items, key=lambda kv: kv[1].total, reverse=True)[:n]

    # --- read: tactical (burst / TTK) -----------------------------------
    def burst(self, seconds: float) -> float:
        """Best damage SUM over any `seconds`-long window of this encounter
        (oopsy/parse-style burst). Scans the per-encounter damage log."""
        if seconds <= 0 or not self.dmg_log:
            return 0.0
        log = self.dmg_log
        best = 0.0
        running = 0.0
        j = 0
        for i in range(len(log)):
            running += log[i][1]
            while log[i][0] - log[j][0] > seconds:
                running -= log[j][1]
                j += 1
            if running > best:
                best = running
        return best

    def time_to_kill(self, target_hp: float | None, now: float | None = None) -> float | None:
        """Seconds to kill a target at the current damage rate, or None if the
        HP is unknown / there's no damage flowing."""
        if not target_hp or target_hp <= 0:
            return None
        dps = self.current_dps(now)
        return target_hp / dps if dps > 0 else None
