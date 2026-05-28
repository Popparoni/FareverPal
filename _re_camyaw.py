"""RE: find the CAMERA yaw (mouse-look direction), distinct from the movement
heading at +0xB0. The user stands still and rotates the camera with the mouse;
the camera-yaw field sweeps continuously (high total travel, wraps) while the
movement heading holds. Read-only via the hook."""
from __future__ import annotations

import math
import struct
import time

from farever_companion.core.proc import Proc
from farever_companion.core.hl import Hl
from farever_companion.core.player import HookLocator

BLOCK = 0x800
DURATION = 40.0
INTERVAL = 0.1
KNOWN = {0x98: "x", 0xA0: "y", 0xA8: "z", 0xB0: "moveHeading", 0xF8: "cam?"}

p = Proc.attach()
loc = HookLocator(p, Hl(p))
rows: list[bytes] = []
try:
    pb = loc.locate()
    if not pb:
        print("NO PLAYER"); raise SystemExit
    print(f"player @ {pb:#x}; {DURATION:.0f}s — STAND STILL, LOOK AROUND with the MOUSE…",
          flush=True)
    t0 = time.time()
    while time.time() - t0 < DURATION:
        base = loc.pbase()
        raw = p.try_read(base, BLOCK) if base else None
        if raw and len(raw) == BLOCK:
            rows.append(raw)
        time.sleep(INTERVAL)
finally:
    loc.disable()
    p.close()

n = len(rows)
print(f"captured {n} samples")
if n < 20:
    print("too few"); raise SystemExit


def series(off, fmt):
    return [struct.unpack_from(fmt, r, off)[0] for r in rows]


def travel(vs, period):
    t = 0.0
    for a, b in zip(vs, vs[1:]):
        d = b - a
        while d > period / 2:
            d -= period
        while d < -period / 2:
            d += period
        t += abs(d)
    return t


# rank angle-like fields by continuous travel (camera yaw sweeps the most)
cands = []
for off in range(0, BLOCK - 8, 4):
    for fmt, kind in (("<f", "f32"), ("<d", "f64")):
        vs = series(off, fmt)
        if not all(math.isfinite(v) for v in vs):
            continue
        mn, mx = min(vs), max(vs)
        sp = mx - mn
        if sp < 0.5:
            continue
        # radians (period 2pi) or degrees (period 360)
        if -7.2 <= mn and mx <= 7.2:
            tr = travel(vs, 2 * math.pi) / (2 * math.pi)
            unit = "rad"
        elif -370 <= mn and mx <= 370 and sp > 20:
            tr = travel(vs, 360.0) / 360.0
            unit = "deg"
        else:
            continue
        if tr > 0.4:
            cands.append((tr, off, kind, unit, mn, mx, sp, vs))

cands.sort(reverse=True)
print(f"\n{'off':>6} {'kind':>4} {'unit':>4} {'travel':>7} {'min':>8} {'max':>8} {'span':>7}  series")
for tr, off, kind, unit, mn, mx, sp, vs in cands[:16]:
    step = max(1, n // 14)
    trail = " ".join(f"{v:+.2f}" for v in vs[::step][:14])
    note = f"  <- {KNOWN[off]}" if off in KNOWN else ""
    print(f"{off:#06x} {kind:>4} {unit:>4} {tr:7.2f} {mn:8.2f} {mx:8.2f} {sp:7.2f}  {trail}{note}")
print("\n(camera yaw = highest 'travel' (~your number of full mouse spins), span ~= full circle)")
