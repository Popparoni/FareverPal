"""RE pass 2: confirm the heading field. Track a shortlist at high-res while the
user does smooth one-direction full spins IN PLACE. The true heading sweeps its
full range and WRAPS once per revolution; timers ramp monotonically; vectors
oscillate in [-1,1]."""
from __future__ import annotations

import math
import struct
import time

from farever_companion.core.proc import Proc
from farever_companion.core.hl import Hl
from farever_companion.core.player import HookLocator

BLOCK = 0x600
DURATION = 30.0
INTERVAL = 0.1
SHORTLIST = [
    (0x0b0, "<d", "f64"), (0x188, "<d", "f64"), (0x170, "<d", "f64"),
    (0x2e8, "<d", "f64"), (0x2f0, "<d", "f64"), (0x478, "<d", "f64"),
    (0x174, "<f", "f32"), (0x2ec, "<f", "f32"), (0x2f4, "<f", "f32"),
    (0x47c, "<f", "f32"),
]

p = Proc.attach()
loc = HookLocator(p, Hl(p))
rows: list[bytes] = []
xyz0 = xyz1 = None
try:
    pb = loc.locate()
    if not pb:
        print("NO PLAYER"); raise SystemExit
    print(f"player @ {pb:#x}; {DURATION:.0f}s — SMOOTH SPIN ONE WAY, in place…", flush=True)
    t0 = time.time()
    while time.time() - t0 < DURATION:
        base = loc.pbase()
        raw = p.try_read(base, BLOCK) if base else None
        if raw and len(raw) == BLOCK:
            rows.append(raw)
            xyz = loc.read_xyz()
            if xyz0 is None:
                xyz0 = xyz
            xyz1 = xyz
        time.sleep(INTERVAL)
finally:
    loc.disable()
    p.close()

n = len(rows)
print(f"captured {n} samples")
if xyz0 and xyz1:
    print(f"position drift: dx={xyz1[0]-xyz0[0]:.2f} dy={xyz1[1]-xyz0[1]:.2f}")


def travel(vs, period):
    """Total unwrapped angular travel assuming `period` (detects wraps)."""
    t = 0.0
    for a, b in zip(vs, vs[1:]):
        d = b - a
        while d > period / 2:
            d -= period
        while d < -period / 2:
            d += period
        t += abs(d)
    return t


print(f"\n{'off':>6} {'kind':>4} {'min':>8} {'max':>8} {'span':>7} {'travel/2pi':>10}  series")
for off, fmt, kind in SHORTLIST:
    vs = [struct.unpack_from(fmt, r, off)[0] for r in rows]
    if not all(math.isfinite(v) for v in vs):
        continue
    mn, mx = min(vs), max(vs)
    # guess period: radians (2pi) is the common case for these ranges
    period = 2 * math.pi
    tr = travel(vs, period) / (2 * math.pi)
    step = max(1, n // 18)
    trail = " ".join(f"{v:+.2f}" for v in vs[::step][:18])
    print(f"{off:#06x} {kind:>4} {mn:8.3f} {mx:8.3f} {mx-mn:7.3f} {tr:10.2f}  {trail}")
print("\n(heading: travel/2pi ~= number of full spins you did; span ~= full period; it WRAPS)")
