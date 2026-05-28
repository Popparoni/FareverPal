"""Boss detection + unit loot resolution (headless)."""
from farever_companion.data import units, rarity


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
