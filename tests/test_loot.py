"""Loot predictor + chest resolution (headless, no game)."""
import math

import pytest

from farever_companion import paths
from farever_companion.data import loot, rarity
from farever_companion.geo import chests


def _game_data_present() -> bool:
    """True if the optional game data this module needs is reachable: the CDB
    sheets (data/sheets/lootTable.json) AND the chest position/index files.
    False when that data isn't present in the checkout -> these data-dependent
    tests skip. They run wherever the data is present (assertions unchanged)."""
    try:
        return ((paths.sheets_dir() / "lootTable.json").exists()
                and paths.chest_positions_path().exists()
                and paths.chest_index_path().exists())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _game_data_present(),
    reason="requires game data (data/sheets/*.json + chest index files)")


def test_predictor_returns_sorted_rows():
    rows = loot.predict_sorted("Kobold", 10)
    assert rows, "Kobold table should produce drops"
    probs = [p for _, p, _, _ in rows]
    assert probs == sorted(probs, reverse=True)
    assert all(isinstance(item, str) for item, *_ in rows)


# --- Weights flag: normalized weighted pick-one (guaranteed) -------------
# The boss bug: MunsterChuck (flags=Weights) lists two 0.01-weight weapons.
# They are 50/50 of a GUARANTEED weapon, not 1%/1% independent rolls.

def test_weights_table_normalizes_to_guaranteed_pick_one():
    out = loot.predict("MunsterChuck", 19)
    assert math.isclose(sum(out.values()), 1.0, abs_tol=1e-9), \
        "a Weights table picks exactly one entry -> probabilities sum to 1"
    assert math.isclose(out["DM_Multispin"], 0.5, abs_tol=1e-9)
    assert math.isclose(out["Mace_Benediction"], 0.5, abs_tol=1e-9)


def test_all_boss_signature_tables_are_guaranteed():
    # Every named boss's signature (bossLootTable) is a Weights pick-one,
    # so its per-item probabilities sum to exactly 1 (a weapon always drops).
    for boss in ("Nepsilon", "Reblochonk", "Gatsbee", "Crabgantua",
                 "MunsterChuck", "Mokshi", "SpongeBlob", "Golcano",
                 "Cleodora", "Ratsar"):
        s = sum(loot.predict(boss, 20).values())
        assert math.isclose(s, 1.0, abs_tol=1e-9), f"{boss} should guarantee a drop"


def test_weighted_subtable_relative_weights():
    # TinProspecting_Items (Weights): 0.01 / 0.01 / 0.004 -> 5/12, 5/12, 1/6.
    out = loot.predict("TinProspecting_Items", 10)
    assert math.isclose(out["Agate"], 5 / 12, abs_tol=1e-6)
    assert math.isclose(out["Ruby"], 5 / 12, abs_tol=1e-6)
    assert math.isclose(out["Malachite"], 1 / 6, abs_tol=1e-6)


def test_weights_normalization_is_over_level_eligible_entries_only():
    # Ores (Weights) has L13+ entries (TinOre, Coal); at L10 they are gated
    # out BEFORE normalization, so the survivors renormalize among themselves.
    out = loot.predict("Ores", 10)
    assert "TinOre" not in out and "Coal" not in out
    # survivors: CopperOre 0.04, Rock_Z1 0.02, TungsteneOre 0.005 (sum 0.065)
    assert math.isclose(out["CopperOre"], 0.04 / 0.065, abs_tol=1e-6)
    assert math.isclose(sum(out.values()), 1.0, abs_tol=1e-9)


def test_non_weights_table_keeps_independent_rolls():
    # Bee is a plain (non-Weights) table: entries roll independently, the
    # listed proba IS the drop chance.
    out = loot.predict("Bee", 10)
    assert math.isclose(out["Wing_Z1"], 0.35, abs_tol=1e-9)
    assert math.isclose(out["Honey_Z1"], 0.15, abs_tol=1e-9)
    # 0.05 -> NatureWeights (single entry -> normalizes to 1.0) -> 5%
    assert math.isclose(out["MoteOfNature"], 0.05, abs_tol=1e-9)


def test_boss_secondary_table_is_a_true_rare_roll():
    # The boss's separate (non-Weights) lootTable carries the genuinely-rare
    # cosmetic at its literal proba.
    out = loot.predict("MunsterChuck_LT2", 19)
    assert math.isclose(out["Mount_Skunk_04"], 0.01, abs_tol=1e-9)


def test_drop_vs_rarity_separation_for_boss_weapon():
    # Compose predict() with the rarity roll exactly like
    # model.drop_table_effective: the weapon is guaranteed (drop% = 100%),
    # the rarity is rolled on top, and a Legendary weapon is ~1% overall.
    base = loot.predict_sorted("MunsterChuck", 19)
    legendary = 0.0
    total = 0.0
    for item, prob, rar, typ in base:
        assert rarity.should_promote(item, typ, rar), "boss weapons roll rarity"
        dist = rarity.promote_distribution(rar, 19)
        total += prob
        legendary += prob * dist.get("Legendary", 0.0)
    assert math.isclose(total, 1.0, abs_tol=1e-9)       # a weapon always drops
    assert math.isclose(legendary, 0.01, abs_tol=1e-6)  # ~1% is Legendary


def test_missing_table_is_explicit():
    out = loot.predict("NoSuchTable_zzz", 10)
    assert any(k.startswith("<missing:") for k in out)


def test_chest_table_resolution_is_deterministic():
    # same id -> same table every call (the anti-flicker contract)
    a = chests.loot_table_for("BossChest_6")
    b = chests.loot_table_for("BossChest_6")
    assert a == b


def test_chests_loaded():
    cl = chests.load_chests()
    assert len(cl) > 100
    assert all(c.chest_id for c in cl)


def test_boss_chest_template_is_a_placeholder():
    # The generic `BossChest` template resolves to a placeholder, NOT a real
    # boss table — LiveModel.chest_table re-attributes it to the live dungeon
    # boss. Specific boss-arena chests carry their own boss table.
    assert chests.loot_table_for("BossChest") == "UpgradeItems_Activity"
    assert chests.loot_table_for("BossChest_Crabgantua_1") == "Crabgantua"
    # an unknown instance boss chest still falls back to the placeholder via the
    # longest-prefix match (so the model's boss re-attribution kicks in)
    assert chests.loot_table_for("BossChest_Cleodora_1") == "UpgradeItems_Activity"
