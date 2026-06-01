"""Encounter snapshot / personal-best / export (headless)."""
import json

from farever_companion.combat.dps import DpsMeter, DamageEvent
from farever_companion.combat import encounter as enc_mod


def _meter():
    m = DpsMeter()
    m.add_event(DamageEvent(amount=300, skill="Slash", crit=True, target="Boss"), now=1.0)
    m.add_event(DamageEvent(amount=100, skill="Slash", target="Boss"), now=2.0)
    m.add_event(DamageEvent(amount=600, skill="Smite", kill=True, target="Boss"), now=3.0)
    m.add_event(DamageEvent(amount=250, skill="Mend", kind="heal", target="Me"), now=2.5)
    m.add_event(DamageEvent(amount=80, skill="Slam", target="Me", incoming=True), now=2.2)
    return m


def test_snapshot_aggregates():
    enc = enc_mod.snapshot(_meter(), boss="Cleodora", deaths=0, ended_at=3.0)
    assert enc.boss == "Cleodora"
    assert enc.duration == 2.0           # first damage 1.0 -> last 3.0
    assert enc.damage_total == 1000
    assert enc.heal_total == 250
    assert enc.taken_total == 80
    assert enc.kills == 1
    assert enc.dps == 500.0              # 1000 / 2s
    # damage skills only in the damage rows; ordered by total
    dmg_rows = [r for r in enc.skills if r.kind == "damage"]
    assert dmg_rows[0].skill == "Smite" and dmg_rows[0].total == 600
    slash = next(r for r in dmg_rows if r.skill == "Slash")
    assert slash.crit_pct == 0.5 and slash.avg_hit == 200


def test_personal_best_tracks_dps():
    best: dict = {}
    weak = enc_mod.snapshot(_meter(), boss="Cleodora", ended_at=3.0)
    assert enc_mod.update_best(best, weak) is True          # first time = a best
    assert best["Cleodora"]["dps"] == 500.0
    # a stronger run improves it
    m2 = DpsMeter()
    m2.add_event(DamageEvent(amount=2000, skill="Smite", target="Boss"), now=1.0)
    m2.add_event(DamageEvent(amount=2000, skill="Smite", target="Boss"), now=2.0)
    strong = enc_mod.snapshot(m2, boss="Cleodora", ended_at=2.0)
    assert enc_mod.is_new_best(best, strong) is True
    assert enc_mod.update_best(best, strong) is True
    assert best["Cleodora"]["dps"] == 4000.0
    # a weaker rerun does not regress the best
    assert enc_mod.update_best(best, weak) is False
    assert best["Cleodora"]["dps"] == 4000.0


def test_export_json_and_csv(tmp_path):
    enc = enc_mod.snapshot(_meter(), boss="Cleodora", ended_at=3.0)
    j = tmp_path / "run.json"
    enc_mod.export(enc, str(j))
    data = json.loads(j.read_text())
    assert data["boss"] == "Cleodora" and data["damage_total"] == 1000
    assert any(s["skill"] == "Smite" for s in data["skills"])

    c = tmp_path / "run.csv"
    enc_mod.export(enc, str(c))
    text = c.read_text()
    assert text.splitlines()[0].startswith("kind,skill,total")
    assert "Smite" in text
