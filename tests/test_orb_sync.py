"""Fx-based collected-orb detection (glow fx = not collected), debounced,
plus chest open/close state sync."""
from farever_companion.geo.chests import is_crate
from farever_companion.geo.orb_sync import FxSync, sync_chests


def test_is_crate_classification():
    assert is_crate("WorldCrate")
    assert is_crate("BeeCrate") and is_crate("CrimsonCrate")
    assert not is_crate("WorldActivity")
    assert not is_crate("KoboldActivity")
    assert not is_crate("Nepsilon")        # world boss
    assert not is_crate(None)


def test_lootdrop_on_chest_marks_opened():
    from farever_companion.geo.orb_sync import chests_opened_by_drops
    chests = [("WorldChest_63", 100.0, 200.0, 10.0),
              ("WorldChest_64", 160.0, 200.0, 10.0)]
    drops = [(101.5, 201.0, 10.5)]                  # pile on chest 63
    assert chests_opened_by_drops(drops, chests, set()) == ["WorldChest_63"]
    # already marked -> quiet; far drop -> nothing
    assert chests_opened_by_drops(drops, chests, {"WorldChest_63"}) == []
    assert chests_opened_by_drops([(500, 500, 0)], chests, set()) == []


def test_chest_opened_marks_and_respawn_unmarks():
    mark, unmark = sync_chests(
        [("W1_Siagarta_WorldChest_19", "Open"),
         ("Chest_A", "Closed"), ("Chest_B", "Locked")], set())
    assert mark == ["W1_Siagarta_WorldChest_19"]
    assert unmark == []
    # respawned crate un-greys; null state changes nothing
    mark, unmark = sync_chests(
        [("W1_Siagarta_WorldChest_19", "Closed"), ("Chest_C", None)],
        {"W1_Siagarta_WorldChest_19"})
    assert mark == []
    assert unmark == ["W1_Siagarta_WorldChest_19"]


def test_fxless_marks_after_two_ticks():
    s = FxSync()
    mark, unmark = s.update([("RedOrb_World_86", False)], set())
    assert mark == [] and unmark == []          # debounce: first sighting
    mark, unmark = s.update([("RedOrb_World_86", False)], set())
    assert mark == ["RedOrb_World_86"]          # confirmed


def test_glowing_never_marks_and_unmarks_stale():
    s = FxSync()
    mark, unmark = s.update([("RedOrb_World_35", True)], {"RedOrb_World_35"})
    assert mark == []
    assert unmark == ["RedOrb_World_35"]        # glowing = not collected


def test_fx_spawn_lag_resets_debounce():
    s = FxSync()
    s.update([("RedOrb_World_1", False)], set())     # chunk just loaded, no fx yet
    mark, _ = s.update([("RedOrb_World_1", True)], set())
    assert mark == []
    mark, _ = s.update([("RedOrb_World_1", False)], set())
    assert mark == []                            # pending was reset by the glow


def test_unloaded_pending_dropped():
    s = FxSync()
    s.update([("RedOrb_World_2", False)], set())
    s.update([], set())                          # orb unloaded between ticks
    mark, _ = s.update([("RedOrb_World_2", False)], set())
    assert mark == []                            # must re-confirm after reload


def test_already_done_stays_quiet():
    s = FxSync()
    s.update([("RedOrb_World_9", False)], {"RedOrb_World_9"})
    mark, unmark = s.update([("RedOrb_World_9", False)], {"RedOrb_World_9"})
    assert mark == [] and unmark == []
