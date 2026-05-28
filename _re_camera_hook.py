"""RE: capture the camera object via the CT 'fov' site (movsd xmm0,[r8+disp];
r8 = camera), then sample its fields while the user mouse-orbits to find the yaw
(a field sweeping a full circle, or a pos/target direction). Installs a 2nd
read-only-effect hook; restored on exit."""
from __future__ import annotations

import math
import struct
import time

from farever_companion.core.proc import Proc
from farever_companion.core.hl import Hl
from farever_companion.core import inject
from farever_companion.core.scan import find_unique

AOB = "F2 49 0F 10 80 ?? ?? ?? ?? F2 48 0F 11 45 ?? 4D 8B 10"
STOLEN = 9                      # movsd xmm0,[r8+disp32] = F2 49 0F 10 80 + disp32
SLOT_OFF = 7 + STOLEN + 5       # mov(7) + stolen(9) + jmp(5) = 21
BLOCK = 0x200
DUR = 30.0


def build_cam(tramp, hook, original):
    off_jmp = 7 + len(original)
    off_slot = off_jmp + 5
    disp = (tramp + off_slot) - (tramp + 7)
    mov = b"\x4C\x89\x05" + struct.pack("<i", disp)        # mov [rip+disp], r8
    rel = (hook + len(original)) - (tramp + off_jmp + 5)
    jmp = b"\xE9" + struct.pack("<i", rel)
    body = mov + bytes(original) + jmp + struct.pack("<Q", 0)
    assert len(body) == off_slot + 8
    return body


p = Proc.attach()
site = find_unique(p, AOB, executable=True)
print(f"fov site @ {site:#x}; installing camera-capture hook…", flush=True)
hook = inject.Hook(p, site, STOLEN, build_cam)
samples = []
try:
    hook.enable()
    slot = hook.tramp_addr + SLOT_OFF
    # wait for the hook to fire (camera ptr captured)
    cam = None
    for _ in range(60):
        raw = p.try_read(slot, 8)
        v = struct.unpack("<Q", raw)[0] if raw else 0
        if v:
            cam = v; break
        time.sleep(0.05)
    if not cam:
        print("camera ptr not captured (fov not read?)"); raise SystemExit
    print(f"camera object @ {cam:#x}; {DUR:.0f}s — MOUSE-ORBIT now (stand still)…", flush=True)
    t0 = time.time()
    while time.time() - t0 < DUR:
        c = struct.unpack("<Q", p.try_read(slot, 8))[0]      # live camera ptr
        raw = p.try_read(c, BLOCK) if c else None
        if raw and len(raw) == BLOCK:
            samples.append(raw)
        time.sleep(0.1)
finally:
    hook.disable()
    print("camera hook disabled + restored")

n = len(samples)
print(f"captured {n} samples")
if n < 15:
    p.close(); raise SystemExit

def series(off, fmt):
    return [struct.unpack_from(fmt, r, off)[0] for r in samples]

def travel(vs, period):
    t = 0.0
    for a, b in zip(vs, vs[1:]):
        d = b - a
        while d > period/2: d -= period
        while d < -period/2: d += period
        t += abs(d)
    return t

cands = []
for off in range(0, BLOCK-8, 4):
    for fmt, kind in (("<f","f32"),("<d","f64")):
        vs = series(off, fmt)
        if not all(math.isfinite(v) for v in vs): continue
        mn,mx = min(vs),max(vs); sp=mx-mn
        if sp < 0.5: continue
        if -7.2<=mn and mx<=7.2: tr=travel(vs,2*math.pi)/(2*math.pi); u="rad"
        elif -1.06<=mn and mx<=1.06: tr=travel(vs,4)/(2*math.pi); u="unit"   # cos/sin
        else: continue
        if tr>0.3: cands.append((tr,off,kind,u,mn,mx,sp,vs))
cands.sort(reverse=True)
print(f"\n{'off':>6} {'kind':>4} {'u':>4} {'travel':>7} {'min':>8} {'max':>8} {'span':>7}  series")
for tr,off,kind,u,mn,mx,sp,vs in cands[:16]:
    step=max(1,n//12); trail=" ".join(f"{v:+.2f}" for v in vs[::step][:12])
    print(f"{off:#06x} {kind:>4} {u:>4} {tr:7.2f} {mn:8.2f} {mx:8.2f} {sp:7.2f}  {trail}")
p.close()
print("DONE")
