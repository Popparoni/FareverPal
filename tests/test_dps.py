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
