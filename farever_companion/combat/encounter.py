"""Encounter snapshots, personal-best parses, and export — pure + testable.

A "cycle" the DPS overlay already tracks becomes a named **Encounter** here: a
frozen record of one fight (boss, duration, DPS/HPS/DTPS, kills, deaths, the
per-skill table) that can be reviewed, compared to a local personal best, and
exported to CSV/JSON for the wiki/datamining goal.

No process reads, no Qt — fed a `DpsMeter` and a boss label, it produces plain
data. Self-DPS only, client-reported (see CLAUDE.md); a best here is a *local*
personal best, never a server-verified parse.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field, asdict


@dataclass
class SkillRow:
    kind: str
    skill: str
    total: float
    hits: int
    crits: int
    kills: int
    max_hit: float
    min_hit: float

    @property
    def avg_hit(self) -> float:
        return self.total / self.hits if self.hits else 0.0

    @property
    def crit_pct(self) -> float:
        return self.crits / self.hits if self.hits else 0.0


@dataclass
class Encounter:
    boss: str                       # boss/dungeon label, or "" if none detected
    duration: float
    dps: float                      # encounter (sustained) damage / s
    peak_dps: float
    hps: float                      # encounter heal / s
    dtps: float                     # encounter damage-taken / s
    damage_total: float
    heal_total: float
    taken_total: float
    kills: int
    deaths: int
    ended_at: float                 # caller-supplied timestamp (monotonic ok)
    skills: list[SkillRow] = field(default_factory=list)
    # by-target totals (the HP-diff breakdown, when per-skill isn't calibrated yet)
    targets: list[tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # surface derived per-skill reads in the export too
        for sr, raw in zip(self.skills, d["skills"]):
            raw["avg_hit"] = round(sr.avg_hit, 2)
            raw["crit_pct"] = round(sr.crit_pct, 4)
        d["targets"] = [{"name": n, "total": round(t, 2)} for n, t in self.targets]
        return d


def snapshot(meter, boss: str = "", deaths: int = 0,
             ended_at: float = 0.0) -> Encounter:
    """Freeze the live `DpsMeter` into an `Encounter`. Uses encounter (sustained)
    rates, not the instantaneous rolling window, so the record is stable."""
    dur = meter.duration or 0.0

    def per_sec(total: float) -> float:
        return total / dur if dur > 0 else 0.0

    rows = [
        SkillRow(kind=k, skill=sk, total=st.total, hits=st.hits, crits=st.crits,
                 kills=st.kills, max_hit=st.max_hit, min_hit=st.min_hit)
        for (k, sk), st in meter.per_skill.items()
    ]
    rows.sort(key=lambda r: r.total, reverse=True)
    targets = sorted(meter.per_target.items(), key=lambda kv: kv[1], reverse=True)
    return Encounter(
        boss=boss,
        duration=dur,
        dps=per_sec(meter.total),
        peak_dps=meter.peak,
        hps=per_sec(meter.heal_total),
        dtps=per_sec(meter.taken_total),
        damage_total=meter.total,
        heal_total=meter.heal_total,
        taken_total=meter.taken_total,
        kills=meter.kills,
        deaths=deaths,
        ended_at=ended_at,
        skills=rows,
        targets=[(n, t) for n, t in targets],
    )


# --- personal best (local parse) -----------------------------------------
def is_new_best(best: dict, enc: Encounter) -> bool:
    """True if `enc` beats the stored best DPS for its boss. A boss-less or
    zero-DPS encounter never counts."""
    if not enc.boss or enc.dps <= 0:
        return False
    prev = best.get(enc.boss) or {}
    return enc.dps > float(prev.get("dps", 0.0))


def update_best(best: dict, enc: Encounter) -> bool:
    """Record `enc` as the new best for its boss if it wins (per-metric: keeps the
    highest DPS and, separately, the highest HPS ever seen). Mutates `best` in
    place; returns True iff the DPS best improved."""
    if not enc.boss:
        return False
    prev = best.get(enc.boss) or {}
    improved = enc.dps > float(prev.get("dps", 0.0))
    merged = {
        "dps": max(enc.dps, float(prev.get("dps", 0.0))),
        "hps": max(enc.hps, float(prev.get("hps", 0.0))),
        "duration": enc.duration if improved else prev.get("duration", enc.duration),
    }
    best[enc.boss] = merged
    return improved


# --- export ---------------------------------------------------------------
def to_json(enc: Encounter, indent: int = 2) -> str:
    return json.dumps(enc.to_dict(), indent=indent)


def to_csv(enc: Encounter) -> str:
    """Per-skill table as CSV (the flat, spreadsheet-friendly artifact). Falls
    back to the by-target breakdown when per-skill isn't calibrated yet, so an
    HP-diff encounter still exports a meaningful table."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["kind", "skill", "total", "hits", "crits", "crit_pct",
                "kills", "max_hit", "min_hit", "avg_hit"])
    for r in enc.skills:
        w.writerow([r.kind, r.skill, round(r.total, 2), r.hits, r.crits,
                    round(r.crit_pct, 4), r.kills, round(r.max_hit, 2),
                    round(r.min_hit, 2), round(r.avg_hit, 2)])
    if not enc.skills:
        for name, total in enc.targets:
            w.writerow(["target", name, round(total, 2), "", "", "", "", "", "", ""])
    return buf.getvalue()


def export(enc: Encounter, path: str) -> str:
    """Write `enc` to `path`; format chosen by extension (.csv -> table, else
    JSON). Returns the path written."""
    text = to_csv(enc) if path.lower().endswith(".csv") else to_json(enc)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path
