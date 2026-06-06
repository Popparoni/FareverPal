"""Collection-tracker data layer (catalog, progress math, filters) — headless."""
from __future__ import annotations

from farever_companion import paths
from farever_companion.data import collections as col


def test_catalog_loads_with_expected_categories():
    cat = col.catalog()
    assert cat, "collection_catalog.json missing from the wiki data layer"
    keys = [c["key"] for c in col.categories()]
    assert keys == ["mounts", "gliders", "companions"]


def test_known_category_counts():
    # From the CDB export: 52 mounts, 51 gliders, 60 capturable critters.
    assert len(col.items("mounts")) == 52
    assert len(col.items("gliders")) == 51
    assert len(col.items("companions")) == 60


def test_rows_have_required_fields():
    for r in col.items():
        assert r["id"] and r["name"] and r["category"] and r["subtype"]
        assert "obtainable" in r and "source" in r


def test_icon_sheet_mapping():
    assert col.icon_sheet("companions") == "unit"
    assert col.icon_sheet("mounts") == "item"
    assert col.icon_sheet("nonsense") == "item"


def test_icons_exist_for_nearly_all_items():
    root = paths.icons_dir()
    missing = [r["id"] for r in col.items()
               if not (root / col.icon_sheet(r["category"]) / f"{r['id']}.png").exists()]
    # the known atlas gaps are a couple of unreleased armor pieces
    assert len(missing) <= 5, f"too many missing icons: {missing[:10]}"


def test_summary_counts_obtainable_only():
    s = col.summary(set())
    coll, total = s["_overall"]
    assert coll == 0
    n_unobtainable = sum(1 for r in col.items() if not r["obtainable"])
    assert total == len(col.items()) - n_unobtainable


def test_summary_progress_math():
    one_mount = next(r["id"] for r in col.items("mounts") if r["obtainable"])
    s = col.summary({one_mount})
    assert s["mounts"][0] == 1
    assert s["_overall"][0] == 1
    # an unknown id counts nowhere
    s2 = col.summary({"NotARealItem"})
    assert s2["_overall"][0] == 0


def test_matches_filters():
    row = next(r for r in col.items("mounts") if r["obtainable"])
    assert col.matches(row)
    assert col.matches(row, query=row["name"][:4].lower())
    assert not col.matches(row, query="zzzzzz_no_such_thing")
    assert col.matches(row, subtype=row["subtype"])
    assert not col.matches(row, subtype="NotASubtype")
    assert col.matches(row, state="missing", owned=set())
    assert not col.matches(row, state="collected", owned=set())
    assert col.matches(row, state="collected", owned={row["id"]})
