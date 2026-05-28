"""RE pass 2 (decisive): separate camera/aim yaw from movement heading by only
measuring field variation while the player is STATIONARY. A field that changes
while you stand still (and only mouse-look) is camera/aim driven; the movement
heading (0xB0) stays put when stationary."""
from __future__ import annotations

import math
import struct
import time

from farever_companion.core.proc import Proc
from farever_companion.core.hl import Hl
from farever_companion.core.player import HookLocator

BLOCK = 0x800
DURATION = 35.0
INTERVAL = 0.1
KNOWN = {0xB0: "moveHeading", 0x98: "x", 0xA0: "y"}

p = Proc.attach()
loc = HookLocator(p, Hl(p))
recs: list[tuple[float, float, bytes]] = []
try:
    pb = loc.locate()
    if not pb:
        print("NO PLAYER"); raise SystemExit
    print(f"player @ {pb:#x}; {DURATION:.0f}s — STAND STILL (no WASD), MOUSE-LOOK only…",
          flush=True)
    t0 = time.time()
    while time.time() - t0 < DURATION:
        base = loc.pbase()
        raw = p.try_read(base, BLOCK) if base else None
        xyz = loc.read_xyz()
        if raw and len(raw) == BLOCK and xyz:
            recs.append((xyz[0], xyz[1], raw))
        time.sleep(INTERVAL)
finally:
    loc.disable()
    p.close()

n = len(recs)
print(f"captured {n} samples")
# pairs where the player did NOT move (isolate camera/aim from movement)
still = [(a, b) for a, b in zip(recs, recs[1:])
         if math.hypot(b[0] - a[0], b[1] - a[1]) < 0.15]
print(f"stationary consecutive pairs: {len(still)} / {n - 1}")
if len(still) < 15:
    print("not enough stationary samples — try again, fully still"); raise SystemExit


def wrapdiff(d, period):
    while d > period / 2:
        d -= period
    while d < -period / 2:
        d += period
    return d


cands = []
for off in range(0, BLOCK - 8, 4):
    for fmt, kind in (("<f", "f32"), ("<d", "f64")):
        try:
            vals_all = [struct.unpack_from(fmt, r, off)[0] for _, _, r in recs]
        except struct.error:
            continue
        if not all(math.isfinite(v) for v in vals_all):
            continue
        mn, mx = min(vals_all), max(vals_all)
        period = 2 * math.pi if (-7.2 <= mn and mx <= 7.2) else \
                 (360.0 if (-370 <= mn and mx <= 370 and (mx - mn) > 20) else None)
        if period is None:
            continue
        # variation accumulated ONLY across stationary pairs
        still_travel = 0.0
        for (a, b) in still:
            va = struct.unpack_from(fmt, a[2], off)[0]
            vb = struct.unpack_from(fmt, b[2], off)[0]
            still_travel += abs(wrapdiff(vb - va, period))
        if still_travel > 1.0:
            cands.append((still_travel, off, kind, period, mn, mx))

cands.sort(reverse=True)
print(f"\nfields that change WHILE STATIONARY (camera/aim yaw ranks top):")
print(f"{'off':>6} {'kind':>4} {'still_travel':>12} {'min':>8} {'max':>8} {'period':>7}")
for st, off, kind, period, mn, mx in cands[:16]:
    note = f"  <- {KNOWN[off]}" if off in KNOWN else ""
    print(f"{off:#06x} {kind:>4} {st:12.2f} {mn:8.2f} {mx:8.2f} {'2pi' if period<10 else 'deg':>7}{note}")
