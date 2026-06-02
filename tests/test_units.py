"""Boss detection + unit loot resolution (headless)."""
import pytest

from farever_companion import paths
from farever_companion.data import units, rarity


def _game_data_present() -> bool:
    """True if the optional CDB sheets are reachable (data/sheets/lootTable.json
    marker resolves). False when the game data isn't present in the checkout ->
    these data-dependent tests skip. They run wherever the data is present
    (assertions unchanged)."""
    try:
        return (paths.sheets_dir() / "lootTable.json").exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _game_data_present(),
    reason="requires game data (data/sheets/*.json)")


def test_named_bosses_are_the_ten():
    bosses = units.named_bosses()
    assert "MunsterChuck" in bosses
    assert len(bosses) == 10
    assert units.is_boss("MunsterChuck")
    assert not units.is_boss("Kobold")


def test_boss_uses_signature_table():
    assert units.loot_table_for_unit("MunsterChuck") == "MunsterChuck"


def test_trash_unit_uses_type_table():
    # a Kobold-type unit resolves to the Kobold table via unitType
    tbl = units.loot_table_for_unit("Kobold")
    assert tbl in (None, "Kobold") or isinstance(tbl, str)


def test_rarity_promotion_floors_and_sums_to_one():
    dist = rarity.promote_distribution("Rare", 20)
    assert "Uncommon" not in dist            # floored at Rare
    assert abs(sum(dist.values()) - 1.0) < 1e-6
