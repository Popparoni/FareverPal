"""DPS engine logic (headless)."""
from farever_companion.combat.dps import DpsMeter, DamageEvent


def test_hp_diff_accumulates_and_windows():
    m = DpsMeter()
    m.update([(1, "Boss", 1000.0)], now=1.0)
    m.update([(1, "Boss", 900.0)], now=2.0)   # 100
    m.update([(1, "Boss", 700.0)], now=3.0)   # 200
    assert m.total == 300
    assert m.current_dps(3.0) == 300 / m.WINDOW


def test_kill_credit_on_near_dead_despawn():
    m = DpsMeter()
    m.update([(1, "Boss", 1000.0)], now=1.0)
    m.update([(1, "Boss", 120.0)], now=2.0)    # nearly dead, recently damaged
    m.update([], now=2.5)                       # despawn -> killing blow
    assert m.kills == 1
    assert m.total == 1000


def test_leaving_range_is_not_a_kill():
    m = DpsMeter()
    m.update([(1, "Boss", 1000.0)], now=1.0)
    m.update([(1, "Boss", 950.0)], now=2.0)     # only lightly damaged
    m.update([], now=2.5)                        # walked away, not killed
    assert m.kills == 0


def test_events_give_per_skill_and_crit():
    m = DpsMeter()
    m.add_event(DamageEvent(amount=456, skill="Fireball", crit=True, target="K"), now=1.0)
    m.add_event(DamageEvent(amount=200, skill="Fireball", target="K"), now=1.5)
    m.add_event(DamageEvent(amount=999, skill="Meteor", crit=True, kill=True, target="K"), now=2.0)
    assert m.has_events
    top = dict((k, v) for k, v in m.top_skills())
    assert round(top["Fireball"].total) == 656
    assert top["Fireball"].crits == 1 and top["Fireball"].hits == 2
    assert top["Meteor"].kills == 1
    assert m.kills == 1


def test_add_taken_feeds_dtps_not_per_skill():
    m = DpsMeter()
    m.add_taken(120.0, now=1.0)
    m.add_taken(80.0, now=2.0)
    assert m.taken_total == 200
    assert m.dtps(now=2.0) == 200 / m.WINDOW       # both within the 5s window
    assert len(m.recent_incoming) == 2             # feeds the death recap
    # taken must NOT flip the meter into per-skill (outgoing) mode
    assert m.has_events is False
    assert m.total == 0 and not m.per_skill
    assert m.current_dps(2.0) == 0                 # taken never counts as your DPS


def test_add_taken_ignores_nonpositive():
    m = DpsMeter()
    m.add_taken(0.0, now=1.0)
    m.add_taken(-50.0, now=1.0)
    assert m.taken_total == 0 and not m.incoming


def test_skillstat_derived_avg_min_max_crit():
    m = DpsMeter()
    m.add_event(DamageEvent(amount=100, skill="Slash", crit=True, target="K"), now=1.0)
    m.add_event(DamageEvent(amount=300, skill="Slash", target="K"), now=1.2)
    st = m.per_skill_of("damage")["Slash"]
    assert st.hits == 2 and st.total == 400
    assert st.min_hit == 100 and st.max_hit == 300
    assert st.avg_hit == 200
    assert st.crit_pct == 0.5


def test_typed_kinds_separate_from_damage():
    m = DpsMeter()
    m.add_event(DamageEvent(amount=500, skill="Strike", target="K"), now=1.0)
    m.add_event(DamageEvent(amount=200, skill="Mend", kind="heal", target="Me"), now=1.1)
    m.add_event(DamageEvent(amount=80, skill="Ward", kind="shield", target="Me"), now=1.2)
    # damage-only headline ignores heal/shield
    assert m.current_dps(1.2) == 500 / m.WINDOW
    assert m.hps(1.2) == 200 / m.WINDOW
    assert m.sps(1.2) == 80 / m.WINDOW
    assert m.total == 500 and m.heal_total == 200 and m.shield_total == 80
    # the damage table must not contain the heal/shield skills
    assert set(m.per_skill_of("damage")) == {"Strike"}
    assert set(m.per_skill_of("heal")) == {"Mend"}
    assert m.has_kind("heal") and m.has_kind("shield") and not m.has_kind("xyz")


def test_incoming_feeds_dtps_not_damage():
    m = DpsMeter()
    m.add_event(DamageEvent(amount=400, skill="Strike", target="K"), now=1.0)
    m.add_event(DamageEvent(amount=150, skill="BossSlam", target="Me",
                            incoming=True), now=1.5)
    assert m.current_dps(1.5) == 400 / m.WINDOW       # incoming excluded
    assert m.dtps(1.5) == 150 / m.WINDOW
    assert m.taken_total == 150
    assert m.total == 400
    assert len(m.recent_incoming) == 1


def test_burst_window_and_ttk():
    m = DpsMeter()
    # 1000 over the first second, then a gap, then 100
    for t in (1.0, 1.2, 1.4, 1.6, 1.8):
        m.add_event(DamageEvent(amount=200, skill="Spin", target="K"), now=t)
    m.add_event(DamageEvent(amount=100, skill="Spin", target="K"), now=5.0)
    assert m.burst(1.0) == 1000        # best 1s window = the opening flurry
    assert m.burst(0.5) == 600         # 1.4,1.6,1.8 within 0.5s
    # TTK at the live rate: current_dps over the 5s window at t=1.8
    dps = m.current_dps(1.8)
    assert m.time_to_kill(dps * 2.0, now=1.8) == 2.0
    assert m.time_to_kill(None) is None
