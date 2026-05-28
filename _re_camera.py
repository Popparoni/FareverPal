"""RE pass A (read-only): reach the Heaps camera object via the HL type system,
the same bootstrap we use for ent.Hero. Find h3d.Camera instances and dump their
first fields (pos/target/dir vectors) so pass B can identify the yaw live."""
from __future__ import annotations

import struct

from farever_companion.core.proc import Proc
from farever_companion.core.hl import Hl, _HOBJ

TO_NAME = 0x10


def u16z(s):
    return s.encode("utf-16-le") + b"\x00\x00"


def find_type(proc, hl, type_name):
    # one full sweep for the class-name string, then HEAP-ONLY (rw_only) ref
    # scans (the slow part if it touches the mapped paks). tight caps.
    names = proc.find_bytes(u16z(type_name), align=2, rw_only=False, max_hits=6)
    print(f"  name hits: {[hex(a) for a in names]}", flush=True)
    for na in names:
        for ref in proc.find_qword(na, rw_only=True, max_hits=8):
            type_obj = ref - TO_NAME
            for tref in proc.find_qword(type_obj, rw_only=True, max_hits=8):
                cand = tref - 8
                try:
                    if hl.i32(cand) == _HOBJ and hl.type_name(cand) == type_name:
                        return cand
                except Exception:
                    continue
    return None


p = Proc.attach()
hl = Hl(p)
for tn in ("h3d.Camera",):
    print(f"bootstrapping type {tn!r}…", flush=True)
    tp = find_type(p, hl, tn)
    print(f"  type ptr: {tp:#x}" if tp else "  NOT FOUND")
    if not tp:
        continue
    inst = p.find_qword(tp, rw_only=True, max_hits=16)
    print(f"  {len(inst)} instance(s): {[hex(a) for a in inst[:6]]}")
    for a in inst[:3]:
        raw = p.try_read(a, 0x90)
        if not raw:
            continue
        f32 = struct.unpack_from("<" + "f" * (0x90 // 4), raw, 0)
        print(f"  inst {a:#x} f32:")
        for i, v in enumerate(f32):
            if -100000 < v < 100000 and (abs(v) > 1e-4 or v == 0):
                print(f"    +{i*4:#04x}: {v:.3f}")
p.close()
print("DONE")
