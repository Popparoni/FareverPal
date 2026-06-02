"""Rarity-promotion model for upgrade-voucher and equipment loot.

A dropped Upgrade<Tier> voucher or rolled-rarity equipment has its rarity picked
at drop time: a single weighted pick from rarity.props.generationChance for the
level bracket, floored at the voucher's tier (sub-floor weight folds up).
"""
from __future__ import annotations

from functools import lru_cache

from . import cdb

ROLL_TIERS = ["Uncommon", "Rare", "Epic", "Legendary"]
_EQUIP_ROOTS = {"MainhandWeapon", "OffhandWeapon", "Gear", "Armor", "Collection"}


@lru_cache(maxsize=1)
def _gen_chance() -> dict[str, list[tuple[int, int, float]]]:
    out: dict[str, list[tuple[int, int, float]]] = {}
    for r in cdb.lines("rarity"):
        gc = (r.get("props") or {}).get("generationChance")
        if gc:
            out[r["id"]] = [(b["minLevel"], b["maxLevel"], b["chance"]) for b in gc]
    return out


def tier_chances(level: int) -> dict[str, float]:
    """Raw generationChance per roll tier for the level's bracket."""
    gc = _gen_chance()
    out = {}
    for tier in ROLL_TIERS:
        for lo, hi, ch in gc.get(tier, []):
            if lo <= level <= hi:
                out[tier] = ch
                break
        else:
            out[tier] = 0.0
    return out


def promote_distribution(floor_rarity: str, level: int) -> dict[str, float]:
    """Outcome rarity distribution for an Upgrade<floor> voucher at `level`.
    Weights below the floor fold up into the floor tier; normalised."""
    base = tier_chances(level)
    if floor_rarity not in ROLL_TIERS:
        return base
    fi = ROLL_TIERS.index(floor_rarity)
    folded = sum(base.get(t, 0.0) for t in ROLL_TIERS[:fi])
    out = {t: base.get(t, 0.0) for t in ROLL_TIERS[fi:]}
    if out:
        out[floor_rarity] = out.get(floor_rarity, 0.0) + folded
    total = sum(out.values())
    if total > 0:
        out = {k: v / total for k, v in out.items()}
    return out


def is_voucher(item_id: str | None, item_type: str | None, rarity: str | None) -> bool:
    return bool(item_id and item_id.startswith("Upgrade")
                and item_type == "Misc" and rarity in ROLL_TIERS)


@lru_cache(maxsize=1)
def _equipment_types() -> frozenset[str]:
    parent = {r["id"]: r.get("inherit") for r in cdb.lines("itemType")}

    def is_eq(tid: str) -> bool:
        cur, seen = tid, set()
        while cur and cur not in seen:
            if cur in _EQUIP_ROOTS:
                return True
            seen.add(cur)
            cur = parent.get(cur)
        return False

    return frozenset(t for t in parent if is_eq(t))


def is_equipment(item_type: str | None) -> bool:
    return bool(item_type) and item_type in _equipment_types()


def should_promote(item_id: str | None, item_type: str | None,
                   rarity: str | None) -> bool:
    """True if this drop's rarity is rolled (vouchers + equipment)."""
    if rarity not in ROLL_TIERS:
        return False
    return is_voucher(item_id, item_type, rarity) or is_equipment(item_type)
