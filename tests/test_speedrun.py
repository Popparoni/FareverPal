"""Speedrun timer auto-stop / cancel logic (headless).

Guards the leaderboard-integrity bug: leaving the dungeon (boss despawns at full
HP, e.g. going to the main menu) must CANCEL the run, never auto-finish + upload.
"""
from farever_companion.core.speedrun import AutoStarter, ModeLatch
from farever_companion.core.speedrun import SpeedrunTimer as T


def _running():
    t = T()
    t.start()
    return t


def test_leaving_dungeon_at_full_hp_is_not_a_kill():
    t = _running()
    assert t.feed_boss("MunsterChuck", True, 21000.0) == t.ALIVE   # entered, boss healthy
    # Main menu / left the dungeon: boss despawns while still at full HP.
    assert t.feed_boss("MunsterChuck", False, None) == t.LEFT
    assert t.state != t.DONE          # caller cancels; timer must NOT have finished
    assert t.is_kill is False


def test_kill_when_boss_dies_in_scene():
    t = _running()
    t.feed_boss("MunsterChuck", True, 21000.0)
    t.feed_boss("MunsterChuck", True, 4000.0)
    assert t.feed_boss("MunsterChuck", True, 0.0) == t.KILL   # corpse at 0 HP
    assert t.state == t.DONE
    assert t.is_kill is True


def test_kill_when_boss_despawns_near_death():
    t = _running()
    t.feed_boss("MunsterChuck", True, 21000.0)
    t.feed_boss("MunsterChuck", True, 300.0)        # < 5% of max → death's door
    assert t.feed_boss("MunsterChuck", False, None) == t.KILL
    assert t.state == t.DONE and t.is_kill is True


def test_despawn_at_high_hp_is_left_not_kill():
    t = _running()
    t.feed_boss("Cleodora", True, 5000.0)
    t.feed_boss("Cleodora", True, 4000.0)           # still 80% — clearly alive
    assert t.feed_boss("Cleodora", False, None) == t.LEFT
    assert t.state != t.DONE and t.is_kill is False


def test_manual_stop_is_not_a_kill():
    t = _running()
    t.feed_boss("MunsterChuck", True, 21000.0)
    t.stop()                                         # hotkey / button
    assert t.state == t.DONE
    assert t.is_kill is False                        # so it won't auto-upload / set PB


def test_boss_never_seen_then_vanishes_does_not_finish():
    t = _running()
    # boss read fails (None) before we ever confirm it alive, then "vanishes"
    assert t.feed_boss(None, False, None) == t.ALIVE
    assert t.state == t.RUNNING


# --- AutoStarter: consistent "starts on real movement" -----------------------

def _settle(a, base=(0.0, 0.0, 0.0)):
    """Feed enough stationary ticks at `base` to establish the baseline."""
    for _ in range(AutoStarter.SETTLE_TICKS + 1):
        assert a.feed(True, base) is False


def test_autostart_never_arms_outside_dungeon():
    a = AutoStarter()
    for _ in range(20):
        assert a.feed(False, (1000.0 * _, 0.0, 0.0)) is False   # walking in town


def test_autostart_fires_only_after_real_movement_from_settled_spawn():
    a = AutoStarter()
    _settle(a, (10.0, 10.0, 0.0))
    # tiny jitter at spawn must NOT start
    assert a.feed(True, (10.2, 10.0, 0.0)) is False
    # deliberate walk away → start, exactly once
    assert a.feed(True, (13.0, 10.0, 0.0)) is True
    assert a.feed(True, (16.0, 10.0, 0.0)) is False   # already started; doesn't re-fire


def test_autostart_ignores_teleport_in_then_starts_on_walk():
    a = AutoStarter()
    # arrive: first tick at the load origin, then a big teleport jump to the spawn
    assert a.feed(True, (0.0, 0.0, 0.0)) is False
    assert a.feed(True, (500.0, 500.0, 0.0)) is False   # teleport → no baseline yet
    # settle at the real spawn, then walk
    _settle(a, (500.0, 500.0, 0.0))
    assert a.feed(True, (504.0, 500.0, 0.0)) is True


def test_autostart_does_not_fire_on_load_jitter_alone():
    a = AutoStarter()
    # continuous small drift that never settles (loading) must not baseline/start
    fired = any(a.feed(True, (float(i), 0.0, 0.0)) for i in range(1, 30))
    assert fired is False


def test_autostart_ignores_teleport_in_landing_drift():
    # Real trace: after the teleport into a dungeon the player spawns above the
    # floor and SETTLES DOWN ~2.2 units in Z over ~1s (X/Y fixed). Each tick of
    # that drop is a tiny step that the old per-tick test mistook for 'still', so
    # it baselined mid-fall and the remaining settle tripped the move threshold —
    # a false start with no real input.
    a = AutoStarter()
    a.feed(True, (2078.0, 162.0, -292.0))        # out in the world
    a.feed(True, (-98.8, 347.8, 17.4))           # teleport into the dungeon (>50)
    z = 17.4
    fired = False
    for _ in range(26):                          # the landing drop (~0.085/tick)
        z -= 0.085
        fired = fired or a.feed(True, (-98.8, 347.8, z))
    assert fired is False                        # must NOT auto-start during the landing
    # genuinely at rest now → settle, then a deliberate walk starts it
    for _ in range(AutoStarter.SETTLE_TICKS + 5):
        a.feed(True, (-98.8, 347.8, z))
    assert a.feed(True, (-95.0, 347.8, z)) is True


def test_autostart_disarms_when_leaving_dungeon():
    a = AutoStarter()
    _settle(a, (10.0, 10.0, 0.0))
    assert a.feed(False, (10.0, 10.0, 0.0)) is False   # left dungeon → reset
    # back in a new dungeon: must re-settle before it can start again
    assert a.feed(True, (10.0, 10.0, 0.0)) is False


# --- ModeLatch: difficulty stays correct through the boss dying --------------

def test_modelatch_prefers_fresh_auto():
    m = ModeLatch()
    m.observe("hard", "auto")
    assert m.resolve("normal", "auto") == ("normal", "auto")   # fresh auto wins


def test_modelatch_falls_back_to_latched_auto_when_finish_read_fails():
    m = ModeLatch()
    m.observe("hard", "auto")          # read reliably mid-fight
    # at finish the boss is dead → fresh read is the manual fallback
    assert m.resolve("normal", "manual") == ("hard", "auto")


def test_modelatch_uses_manual_when_never_auto_detected():
    m = ModeLatch()
    assert m.resolve("normal", "manual") == ("normal", "manual")


def test_modelatch_does_not_latch_manual_or_unknown():
    m = ModeLatch()
    m.observe("hard", "manual")        # manual reads must not latch
    m.observe(None, "auto")            # unreadable
    assert m.resolve("normal", "manual") == ("normal", "manual")


def test_modelatch_reset_clears_previous_run():
    m = ModeLatch()
    m.observe("hard", "auto")
    m.reset()                          # new run
    assert m.resolve("normal", "manual") == ("normal", "manual")
