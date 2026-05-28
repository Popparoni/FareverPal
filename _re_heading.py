"""RE scratch: find the player's facing/heading field by sampling the struct
while the user rotates in place. Standing still -> position stays constant and
only the heading-type fields sweep. Read-only via the hook (auto-restored)."""
from __future__ import annotations

import math
import struct
import time

from farever_companion.core.proc import Proc
from farever_companion.core.hl import Hl
from farever_companion.core.player import HookLocator

BLOCK = 0x600
DURATION = 40.0
INTERVAL = 0.2
KNOWN = {0x98: "x", 0xA0: "y", 0xA8: "z", 0xF8: "cam"}

p = Proc.attach()
loc = HookLocator(p, Hl(p))
samples: list[tuple[float, tuple, bytes]] = []
try:
    pb = loc.locate()
    if not pb:
        print("NO PLAYER (hook didn't fire — in world?)")
        raise SystemExit
    print(f"player @ {pb:#x}; sampling {DURATION:.0f}s — ROTATE IN PLACE now…", flush=True)
    t0 = time.time()
    while time.time() - t0 < DURATION:
        base = loc.pbase()
        raw = p.try_read(base, BLOCK) if base else None
        xyz = loc.read_xyz()
        if raw and len(raw) == BLOCK:
            samples.append((time.time() - t0, xyz, raw))
        time.sleep(INTERVAL)
finally:
    loc.disable()
    p.close()

n = len(samples)
print(f"captured {n} samples")
if n < 10:
    print("too few samples"); raise SystemExit

# did the player actually stand still? (so position fields don't masquerade)
xs = [s[1][0] for s in samples if s[1]]
ys = [s[1][1] for s in samples if s[1]]
zs = [s[1][2] for s in samples if s[1]]
def span(a): return (max(a) - min(a)) if a else 0.0
print(f"position drift: x={span(xs):.2f} y={span(ys):.2f} z={span(zs):.2f} "
      f"(want ~0 — stood still)")

def series(off, fmt):
    out = []
    for _, _, raw in samples:
        try:
            out.append(struct.unpack_from(fmt, raw, off)[0])
        except struct.error:
            pass
    return out

cands = []
for off in range(0, BLOCK - 8, 4):
    for fmt, size, kind in (("<f", 4, "f32"), ("<d", 8, "f64")):
        vs = series(off, fmt)
        if len(vs) < n * 0.8:
            continue
        if not all(math.isfinite(v) for v in vs):
            continue
        sp = span(vs)
        if sp < 0.2:
            continue
        mn, mx = min(vs), max(vs)
        rad = -7.0 <= mn and mx <= 7.0 and sp > 0.5            # radians-ish
        deg = -370 <= mn and mx <= 370 and sp > 10             # degrees-ish
        unit = -1.06 <= mn and mx <= 1.06 and sp > 0.3         # cos/sin component
        if rad or deg or unit:
            tag = "RAD" if rad else ("DEG" if deg else "UNIT")
            cands.append((sp, off, kind, mn, mx, tag, vs))

cands.sort(reverse=True)
print(f"\n{len(cands)} angle-like candidates (excluding tiny/constant fields):")
print(f"{'off':>6} {'kind':>4} {'tag':>4} {'min':>9} {'max':>9} {'span':>9}   series(every ~2s)")
for sp, off, kind, mn, mx, tag, vs in cands[:24]:
    note = f"  <- {KNOWN[off]}" if off in KNOWN else ""
    step = max(1, len(vs) // 12)
    trail = " ".join(f"{v:+.2f}" for v in vs[::step][:12])
    print(f"{off:#06x} {kind:>4} {tag:>4} {mn:9.3f} {mx:9.3f} {sp:9.3f}   {trail}{note}")
