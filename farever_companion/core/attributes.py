"""Read live unit attributes (read-only).

Current Health is a plain f64 at UnitAttributes+0xF0, where
UnitAttributes = unit+0x3D0. Validated live (old project, 2026-05-25) for both
the player (ent.HeroAttributes: 420.0 matched the HP bar) and enemies
(ent.UnitAttributes: 474->444 on a hit, regen back). Same offset across the
Hero/Foe attribute subclasses.

MaxHealth has no confirmed adjacent fixed offset; callers track the highest HP
seen per unit as its max (good enough for DPS + kill detection). Refine if an
exact MaxHealth offset / the attribute IntMap decode lands.
"""
from __future__ import annotations

import struct

from .hl import Hl

OFF_UATTR = 0x3D0     # GameObject -> ent.*Attributes
OFF_HEALTH = 0xF0     # *Attributes + 0xF0 -> current Health (f64)

# Live unit level — an i32 on the UNIT/GameObject struct, right after the
# attributes pointer (OFF_UATTR = 0x3D0). Calibrated + validated live 2026-05-29
# via `_re_level.py`:
#   - Cleodora dungeon: 20 on the boss (CDB 20), 19 on the 3 champions (CDB 19),
#     20 on the player — the ONLY offset that tracked every unit's real level.
#   - Normal/Hard A/B on MunsterChuck (CDB normal 19): read 19 on Normal, 20 on
#     Hard — proving the dungeon bumps the boss level on Hard, which is what
#     detected_mode()'s `live > normal` test keys on.
# Shared across ent.Foe/ent.boss.*/ent.Hero (same GameObject layout), so it works
# for all bosses. Re-run `_re_level.py` if a patch shifts the layout.
OFF_LEVEL: int | None = None          # i32 offset on the ATTRIBUTES struct, or None
OFF_LEVEL_UNIT: int | None = 0x3D8    # i32 offset on the UNIT struct


def health(hl: Hl, unit: int) -> float | None:
    """Current Health of a unit (player or enemy), or None if unreadable."""
    if not unit:
        return None
    ua = hl.ptr(unit + OFF_UATTR)
    if not ua:
        return None
    raw = hl.proc.try_read(ua + OFF_HEALTH, 8)
    if raw is None:
        return None
    hp = struct.unpack("<d", raw)[0]
    if hp != hp or hp < 0 or hp > 1e9:   # NaN or out of range
        return None
    return hp


def _read_i32(hl: Hl, addr: int) -> int | None:
    raw = hl.proc.try_read(addr, 4)
    if raw is None:
        return None
    v = struct.unpack("<i", raw)[0]
    return v if 0 <= v <= 999 else None   # plausible level range


def level(hl: Hl, unit: int) -> int | None:
    """Live level of a unit, or None if unreadable / offset not yet calibrated.
    Tries the attributes-struct offset first, then the unit-struct offset. Both
    are None by default (see OFF_LEVEL notes), so this safely no-ops until one is
    pinned with `_re_level.py`."""
    if not unit:
        return None
    if OFF_LEVEL is not None:
        ua = hl.ptr(unit + OFF_UATTR)
        if ua:
            v = _read_i32(hl, ua + OFF_LEVEL)
            if v is not None:
                return v
    if OFF_LEVEL_UNIT is not None:
        v = _read_i32(hl, unit + OFF_LEVEL_UNIT)
        if v is not None:
            return v
    return None
