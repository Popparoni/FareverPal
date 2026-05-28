"""RE pass 3 (decisive): correlate candidate fields against movement direction.
While the user WALKS a path with turns, the facing field equals atan2(dy,dx)
(up to a fixed sign + offset). Whichever candidate fits with resultant R~1 is
the heading."""
from __future__ import annotations

import math
import struct
import time

from farever_companion.core.proc import Proc
from farever_companion.core.hl import Hl
from farever_companion.core.player import HookLocator

BLOCK = 0x600
DURATION = 25.0
INTERVAL = 0.1
CANDS = [(0x0b0, "<d"), (0x188, "<d"), (0x170, "<d"), (0x174, "<f"),
         (0x2e8, "<d"), (0x2ec, "<f")]

p = Proc.attach()
loc = HookLocator(p, Hl(p))
recs: list[tuple[float, float, dict]] = []   # (x, y, {off:val})
try:
    pb = loc.locate()
    if not pb:
        print("NO PLAYER"); raise SystemExit
    print(f"player @ {pb:#x}; {DURATION:.0f}s — WALK with several turns…", flush=True)
    t0 = time.time()
    while time.time() - t0 < DURATION:
        base = loc.pbase()
        raw = p.try_read(base, BLOCK) if base else None
        xyz = loc.read_xyz()
        if raw and len(raw) == BLOCK and xyz:
            vals = {off: struct.unpack_from(fmt, raw, off)[0] for off, fmt in CANDS}
            recs.append((xyz[0], xyz[1], vals))
        time.sleep(INTERVAL)
finally:
    loc.disable()
    p.close()

n = len(recs)
print(f"captured {n} samples")


def wrap(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


# movement direction between consecutive moving samples
pairs = []   # (move_angle, {off:val})
for (x0, y0, v0), (x1, y1, _v1) in zip(recs, recs[1:]):
    dx, dy = x1 - x0, y1 - y0
    if math.hypot(dx, dy) > 0.4:                 # only when actually moving
        pairs.append((math.atan2(dy, dx), v0))
print(f"moving pairs: {len(pairs)}")
if len(pairs) < 8:
    print("not enough movement captured — walk more next time"); raise SystemExit


def fit(off):
    best = None
    for s in (1, -1):
        diffs = [wrap(v[off] - s * mv) for mv, v in pairs]
        C = sum(math.cos(d) for d in diffs)
        S = sum(math.sin(d) for d in diffs)
        R = math.hypot(C, S) / len(diffs)         # 1.0 == perfect fit
        if best is None or R > best[0]:
            best = (R, s, math.degrees(math.atan2(S, C)))
    return best


print(f"\n{'off':>6} {'R(fit)':>7} {'sign':>5} {'offset°':>8}   (R~1.0 => this IS the heading)")
results = []
for off, _fmt in CANDS:
    R, s, c = fit(off)
    results.append((R, off, s, c))
for R, off, s, c in sorted(results, reverse=True):
    print(f"{off:#06x} {R:7.3f} {s:>5} {c:8.1f}")
