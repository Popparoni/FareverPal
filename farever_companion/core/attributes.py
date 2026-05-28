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

from .hl import Hl

OFF_UATTR = 0x3D0     # GameObject -> ent.*Attributes
OFF_HEALTH = 0xF0     # *Attributes + 0xF0 -> current Health (f64)


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
    import struct
    hp = struct.unpack("<d", raw)[0]
    if hp != hp or hp < 0 or hp > 1e9:   # NaN or out of range
        return None
    return hp
