"""Single source of truth for HashLink layout + game struct offsets.

Every offset lives here exactly once and is imported by the readers (`hl.py`,
`scene.py`, `attributes.py`, `player.py`, `appsingleton.py`) so a concept has
one name and one value. Values are validated against a specific Farever build
and resolved by class name through `hl.py` reflection where practical; the
build-validated dates stay with the readers that document each group.

Calibration-pending offsets (e.g. the per-skill level offset, the isMe path)
stay as `None`-defaulted constants in their own module — they're not shared, so
they don't belong here.
"""
from __future__ import annotations

# --- HashLink runtime object model -----------------------------------------
# instance[+0] -> hl_type*; hl_type[+0]=kind(i32); hl_type[+8] -> hl_type_obj*.
HOBJ = 11            # hl_type kind: object
HSTRUCT = 21         # hl_type kind: struct
USER_MIN = 0x10000           # plausible user-space pointer range (is_ptr guard)
USER_MAX = 0x7FFFFFFFFFFF

# hl_type_obj fields
TO_NAME = 0x10       # -> name (uchar*, UTF-16LE, null-terminated)
TO_SUPER = 0x18      # -> super (hl_type*)
TO_FIELDS = 0x20     # -> fields (hl_obj_field[])
TO_GLOBALVAL = 0x38  # -> &globals_data slot (the type's static $Class object)
FIELD_STRIDE = 0x18  # sizeof(hl_obj_field) on 64-bit

# ArrayObj[+8]=length(i32); ArrayObj[+0x10] -> NativeArray
NA_SIZE_OFF = 0x10   # NativeArray size (i32)
NA_DATA_OFF = 0x18   # NativeArray data (8B object pointers)

# --- GameObject / unit layout ----------------------------------------------
# ent.Hero / ent.Foe / ent.interactible.* share the ent.GameObject layout, so
# the same offsets read the player, enemies and interactibles.
OFF_GAMELAYER = 0x58     # GameObject -> st.GameLayer
OFF_OWNER = 0x60         # GameObject -> owner (a Hero for player-owned units)
OFF_POS = 0x98           # x,y,z contiguous f64 (the player's own position too)
OFF_HEADING = 0xB0       # facing angle, f64 radians (atan2: 0=+x, CCW)
OFF_UNITID = 0x250       # unit-id String
OFF_UATTR = 0x3D0        # GameObject -> ent.*Attributes
OFF_LEVEL_UNIT = 0x3D8   # live level (i32) on the unit struct
UNIT_BLOCK = 0x258       # batched header read: type(0) .. unit-id(0x250)

# st.GameLayer arrays
OFF_UNITS_ARR = 0x128    # -> ArrayObj of ent.Hero / ent.Foe subclasses
OFF_ELEMS_ARR = 0x120    # -> ArrayObj of interactibles (no units)

# interactible element fields
OFF_ELEMID = 0x268       # element-id String
OFF_ELEMSTATE = 0x290    # element state String

# --- *Attributes struct ----------------------------------------------------
OFF_HEALTH = 0xF0        # *Attributes + 0xF0 -> current Health (f64)
