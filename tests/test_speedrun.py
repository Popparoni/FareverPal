"""Speedrun timer auto-stop / cancel logic (headless).

Guards the leaderboard-integrity bug: leaving the dungeon (boss despawns at full
HP, e.g. going to the main menu) must CANCEL the run, never auto-finish + upload.
"""
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
