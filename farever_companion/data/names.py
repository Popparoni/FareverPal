"""Display-name resolver: internal CDB id -> the game's readable name.

Reuses the wiki data layer (htdocs/assets/data/{items,enemies}.json), the same
id -> name mapping the web wiki renders, e.g.
    DM_Multispin   -> "Twin Pillars of Justice"
    MunsterChuck   -> "Munster Chuck"
    UpgradeRare    -> "Spark Shard"
Falls back to a friendly token label, then to the id itself.
"""
from __future__ import annotations

import re
from functools import lru_cache

from . import cdb, tokens


@lru_cache(maxsize=1)
def _items() -> dict[str, str]:
    return {r["id"]: (r.get("name") or r["id"]) for r in cdb.wiki("items")}


@lru_cache(maxsize=1)
def _units() -> dict[str, str]:
    return {r["id"]: (r.get("name") or r["id"]) for r in cdb.wiki("enemies")}


@lru_cache(maxsize=1)
def _skills() -> dict[str, str]:
    return {r["id"]: (r.get("name") or r["id"]) for r in cdb.wiki("skills")}


def skill_name(skill_id: str | None) -> str | None:
    """Internal skill id (BaseSkill.kind, e.g. 'Priest_Prayer_Smite') -> the
    game's readable name ('Prayer: Smite'), via htdocs/assets/data/skills.json.
    Falls back to a humanized id ('Mace_Base_Attack' -> 'Mace Base Attack')."""
    if not skill_id:
        return skill_id
    nm = _skills().get(skill_id)
    if nm and nm != skill_id:
        return nm
    return humanize(skill_id) or skill_id


def item_name(item_id: str | None) -> str | None:
    if not item_id:
        return item_id
    name = _items().get(item_id)
    if name and name != item_id:
        return name
    return tokens.label(item_id) or name or item_id


def unit_name(unit_id: str | None) -> str | None:
    if not unit_id:
        return unit_id
    return _units().get(unit_id, unit_id)


def any_name(some_id: str | None) -> str | None:
    """Unit name, else item name, else the id unchanged."""
    if not some_id:
        return some_id
    return _units().get(some_id) or _items().get(some_id) or some_id


def humanize(raw: str | None) -> str:
    """Turn a CamelCase / snake id into spaced words: 'WorldCrate' -> 'World
    Crate', 'UpgradeItems_Activity' -> 'Upgrade Items Activity'."""
    if not raw:
        return ""
    s = raw.replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)       # camelCase boundary
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)     # ACRONYMWord boundary
    s = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", s)          # letter|digit boundary
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1)
def _zone_region_names() -> dict[str, str]:
    """Zone-tier code digit -> region display name, e.g. {'1': 'Skover Island',
    '2': 'Valley of Eternal Autumn'} from the zone sheet (Z#_Region.texts.name)."""
    out: dict[str, str] = {}
    try:
        for r in cdb.lines("zone"):
            m = re.fullmatch(r"Z(\d+)_Region", r.get("id", ""))
            if m:
                nm = (r.get("texts") or {}).get("name")
                if nm:
                    out[m.group(1)] = nm
    except Exception:
        pass
    return out


def loot_table_label(tid: str | None) -> str:
    """Readable name for a loot-table id, for the predictor dropdown. Named
    bosses use their display name ('Munster Chuck (Boss)'); zone codes resolve to
    the region name ('Vault_Z2_1' -> 'Vault Valley of Eternal Autumn 1');
    everything else is humanized ('WorldCrate' -> 'World Crate')."""
    if not tid:
        return ""
    from . import units
    if tid in units.named_bosses():
        return f"{unit_name(tid)} (Boss)"
    label = humanize(tid)
    regions = _zone_region_names()
    if regions:                              # Z1/Z2/Z3 -> region name
        label = re.sub(r"\bZ ?([0-9]+)\b",
                       lambda m: regions.get(m.group(1), m.group(0)), label)
    return label
