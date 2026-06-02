"""Live scene reader (read-only, batched).

Walks the scene graph from the player struct to the unit + element lists and
reads every object's class, CDB id, owner and world position. Verified scene
path (Farever EA, 2026-05-24):

    pbase (ent.Hero) +0x58  -> st.GameLayer
    st.GameLayer     +0x128 -> ArrayObj  (units: ent.Hero + ent.Foe subclasses)
    st.GameLayer     +0x120 -> ArrayObj  (elements: interactibles, no units)

Per unit (ent.Unit subclasses share ent.GameObject layout):
    +0x60 owner, +0x98/A0/A8 xyz (f64), +0x250 unit-id String, +0x3D0 attributes

PERFORMANCE: one batched `read_many` pulls a 0x258-byte header block for every
unit at once (turning hundreds of syscalls into one), then fields are parsed in
Python and class/id strings resolved through the cached Hl reflection.

CLASSIFICATION: an enemy is anything whose type descends from `ent.Foe` (via the
HL super-chain) and is not owned by a hero — `ent.foe.Boss` etc. are included.
This is the fix for the old boss-HP bug (it used exact `cls == "ent.Foe"`).
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .hl import Hl, is_ptr
from .proc import Proc

OFF_GAMELAYER = 0x58
OFF_UNITS_ARR = 0x128
OFF_ELEMS_ARR = 0x120
OFF_OWNER = 0x60
OFF_POS = 0x98
OFF_UNITID = 0x250
OFF_ELEMID = 0x268
OFF_ELEMSTATE = 0x290

UNIT_BLOCK = 0x258      # covers type(0) .. unit-id(0x250)

_ELEM_KINDS = {
    "ent.interactible.Gatherable": "gatherable",
    "ent.interactible.Chest": "chest",
    "ent.interactible.Obelisk": "obelisk",
    "ent.interactible.Npc": "npc",
    "ent.interactible.Refresher": "refresher",
    "ent.interactible.Bumper": "bumper",
    "ent.interactible.MobilePlatform": "platform",
    "ent.interactible.RespawnPoint": "respawn",
    "ent.interactible.InstanceOrb": "orb",
}
PLAYER_OWNER_CLASSES = {"ent.Hero"}


@dataclass
class Entity:
    addr: int
    cls: str | None          # leaf class, e.g. "ent.Foe", "ent.foe.Boss"
    unit_id: str | None
    owner_cls: str | None
    x: float
    y: float
    z: float
    is_foe: bool = False     # descends from ent.Foe (super-chain)
    is_hero: bool = False    # descends from ent.Hero

    @property
    def is_player_owned(self) -> bool:
        return self.owner_cls in PLAYER_OWNER_CLASSES

    @property
    def is_enemy(self) -> bool:
        return self.is_foe and not self.is_player_owned

    @property
    def kind(self) -> str:
        if self.is_hero:
            return "hero"
        if self.is_foe:
            return "companion" if self.is_player_owned else "enemy"
        return self.cls or "?"

    def dist(self, x: float, y: float, z: float) -> float:
        return math.dist((self.x, self.y, self.z), (x, y, z))


@dataclass
class Element:
    addr: int
    cls: str | None
    elem_id: str | None
    state: str | None
    x: float
    y: float
    z: float

    @property
    def kind(self) -> str:
        return _ELEM_KINDS.get(self.cls or "", (self.cls or "")
                               .replace("ent.interactible.", "").replace("ent.", ""))

    @property
    def is_gatherable(self) -> bool:
        return self.cls == "ent.interactible.Gatherable"

    @property
    def is_chest(self) -> bool:
        return self.cls == "ent.interactible.Chest"

    @property
    def is_obelisk(self) -> bool:
        return self.cls == "ent.interactible.Obelisk"

    def dist(self, x: float, y: float, z: float) -> float:
        return math.dist((self.x, self.y, self.z), (x, y, z))


class Scene:
    def __init__(self, proc: Proc, hl: Hl):
        self.proc = proc
        self.hl = hl

    def gamelayer(self, pbase: int | None) -> int | None:
        if not pbase:
            return None
        return self.hl.ptr(pbase + OFF_GAMELAYER)

    def units(self, pbase: int | None) -> list[Entity]:
        gl = self.gamelayer(pbase)
        if gl is None:
            return []
        ptrs = [p for p in self.hl.array(self.hl.ptr(gl + OFF_UNITS_ARR)) if is_ptr(p)]
        if not ptrs:
            return []
        blocks = self.proc.read_many(ptrs, UNIT_BLOCK)
        out: list[Entity] = []
        for ptr, blk in zip(ptrs, blocks):
            if blk is None or len(blk) < UNIT_BLOCK:
                continue
            type_ptr = struct.unpack_from("<Q", blk, 0)[0]
            owner_ptr = struct.unpack_from("<Q", blk, OFF_OWNER)[0]
            x, y, z = struct.unpack_from("<ddd", blk, OFF_POS)
            uid_ptr = struct.unpack_from("<Q", blk, OFF_UNITID)[0]
            anc = self.hl.ancestors(type_ptr)
            out.append(Entity(
                addr=ptr,
                cls=self.hl.type_name(type_ptr),
                unit_id=self.hl.hl_string(uid_ptr) if is_ptr(uid_ptr) else None,
                owner_cls=self.hl.class_of(owner_ptr) if is_ptr(owner_ptr) else None,
                x=x, y=y, z=z,
                is_foe="ent.Foe" in anc,
                is_hero="ent.Hero" in anc,
            ))
        return out

    def elements(self, pbase: int | None) -> list[Element]:
        gl = self.gamelayer(pbase)
        if gl is None:
            return []
        ptrs = [p for p in self.hl.array(self.hl.ptr(gl + OFF_ELEMS_ARR)) if is_ptr(p)]
        if not ptrs:
            return []
        block = OFF_ELEMSTATE + 8
        blocks = self.proc.read_many(ptrs, block)
        out: list[Element] = []
        for ptr, blk in zip(ptrs, blocks):
            if blk is None or len(blk) < block:
                continue
            type_ptr = struct.unpack_from("<Q", blk, 0)[0]
            x, y, z = struct.unpack_from("<ddd", blk, OFF_POS)
            eid_ptr = struct.unpack_from("<Q", blk, OFF_ELEMID)[0]
            state_ptr = struct.unpack_from("<Q", blk, OFF_ELEMSTATE)[0]
            out.append(Element(
                addr=ptr,
                cls=self.hl.type_name(type_ptr),
                elem_id=self.hl.hl_string(eid_ptr) if is_ptr(eid_ptr) else None,
                state=self.hl.hl_string(state_ptr) if is_ptr(state_ptr) else None,
                x=x, y=y, z=z,
            ))
        return out
