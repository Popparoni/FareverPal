"""Pure-read player locator — no hook, no writes.

Locates the local player's `ent.Hero` struct entirely by reading + scanning,
using the Rust `find_bytes`. Strategy (write-free, see PLAN §4.2):

  1. Bootstrap the `ent.Hero` hl_type by string:
       a. scan for the UTF-16 class name "ent.Hero\\0"  -> name string addr
       b. scan for a pointer to that name              -> hl_type_obj (name @ +0x10)
       c. scan for a pointer to that type_obj          -> hl_type (obj @ +8), kind=HOBJ
     The type pointer is stable for the process, so it's cached; re-locating on a
     zone change only re-runs step 2.
  2. Scan rw heap for objects whose first qword == that hl_type  -> Hero instances.
  3. Select the local player: prefer `Hero.ownerPlayer.isMe == 1` verified with
     `Player.hero == Hero` (offsets calibrated live — constants below). Until
     calibrated, fall back to the hero with the largest live unit list (the
     loaded world), which is unambiguous in solo play.

Their external scan took ~1 min; the targeted 8-byte type match should be much
faster. Field offsets to calibrate live are isolated as constants.
"""
from __future__ import annotations

import struct
import time

from .hl import Hl, is_ptr, _HOBJ
from .proc import Proc, ProcError
from .scene import OFF_GAMELAYER, OFF_UNITS_ARR
from . import inject
from .scan import find_unique, scan

# --- calibrate live (PLAN §7); None => use the fallback heuristic ---------
OFF_HERO_OWNERPLAYER: int | None = None   # ent.Hero -> st.Player
OFF_PLAYER_ISME: int | None = None        # st.Player.isMe (i32/bool)
OFF_PLAYER_HERO: int | None = None        # st.Player.hero -> ent.Hero
OFF_HERO_ISCOMBAT: int | None = None      # ent.Hero.isInCombat (bool)

TO_NAME = 0x10   # hl_type_obj.name offset (matches hl.TO_NAME)


def _utf16z(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


class PlayerLocator:
    def __init__(self, proc: Proc, hl: Hl | None = None):
        self.proc = proc
        self.hl = hl or Hl(proc)
        self._type_ptr: int | None = None
        self.address: int | None = None

    # --- type bootstrap --------------------------------------------------
    def hero_type(self) -> int | None:
        if self._type_ptr is not None:
            return self._type_ptr
        if not self.proc.has_scan:
            raise ProcError("player locate needs the farever_native scan backend")
        name_hits = self.proc.find_bytes(_utf16z("ent.Hero"), align=2,
                                         rw_only=False, max_hits=64)
        for name_addr in name_hits:
            # hl_type_obj.name points here -> type_obj = (ptr to name_addr) - TO_NAME
            for ref in self.proc.find_qword(name_addr, rw_only=False, max_hits=64):
                type_obj = ref - TO_NAME
                # a hl_type points to type_obj at +8, with kind=HOBJ at +0
                for tref in self.proc.find_qword(type_obj, rw_only=False, max_hits=64):
                    cand = tref - 8
                    try:
                        if self.hl.i32(cand) == _HOBJ and self.hl.type_name(cand) == "ent.Hero":
                            self._type_ptr = cand
                            return cand
                    except ProcError:
                        continue
        return None

    # --- instance scan + selection --------------------------------------
    def _candidates(self) -> list[int]:
        tp = self.hero_type()
        if tp is None:
            return []
        hits = self.proc.find_qword(tp, rw_only=True, max_hits=256)
        # keep ones that are real, fully-formed heroes (valid GameLayer)
        good = []
        for h in hits:
            gl = self.hl.ptr(h + OFF_GAMELAYER)
            if gl is not None:
                good.append(h)
        return good

    def _is_me(self, hero: int) -> bool | None:
        """True/False if the isMe path is calibrated, else None (unknown)."""
        if OFF_HERO_OWNERPLAYER is None or OFF_PLAYER_ISME is None:
            return None
        player = self.hl.ptr(hero + OFF_HERO_OWNERPLAYER)
        if player is None:
            return False
        try:
            isme = self.hl.i32(player + OFF_PLAYER_ISME) == 1
        except ProcError:
            return False
        if OFF_PLAYER_HERO is not None:
            back = self.hl.ptr(player + OFF_PLAYER_HERO)
            if back != hero:
                return False
        return isme

    def _unit_count(self, hero: int) -> int:
        gl = self.hl.ptr(hero + OFF_GAMELAYER)
        if gl is None:
            return -1
        arr = self.hl.ptr(gl + OFF_UNITS_ARR)
        if arr is None:
            return -1
        try:
            return self.hl.i32(arr + 8)
        except ProcError:
            return -1

    def locate(self) -> int | None:
        cands = self._candidates()
        if not cands:
            self.address = None
            return None
        # calibrated path: the verified local player
        verified = [h for h in cands if self._is_me(h) is True]
        if verified:
            self.address = verified[0]
            return self.address
        # fallback: the hero attached to the most populated world
        self.address = max(cands, key=self._unit_count)
        return self.address

    def relocate(self) -> int | None:
        """Re-find the instance after a zone change (type stays cached)."""
        return self.locate()

    # --- reads -----------------------------------------------------------
    def pbase(self) -> int | None:
        return self.address

    def read_xyz(self) -> tuple[float, float, float] | None:
        if not self.address:
            return None
        raw = self.proc.try_read(self.address + 0x98, 24)
        if raw is None:
            return None
        return struct.unpack("<ddd", raw)

    def in_combat(self) -> bool | None:
        if not self.address or OFF_HERO_ISCOMBAT is None:
            return None
        try:
            return self.hl.i32(self.address + OFF_HERO_ISCOMBAT) != 0
        except ProcError:
            return None


# ---------------------------------------------------------------------------
#  HookLocator — fast read-only-EFFECT code hook (default; see core/inject.py)
# ---------------------------------------------------------------------------
# Same unique site the old tool + Farever.CT use: at this instruction r10 holds
# the live player struct (the method's `this`). The trampoline copies r10 into
# our slot every frame, so `pbase()` is always the *current* pointer — robust
# across GC moves and zone changes, unlike a one-shot scanned address.
#
#   AOB: 4D 8B 1A 4D 8B 5B ?? 49 8B CA 8B 55 ?? 4D 8B C1
#   stolen 7 bytes = mov r11,[r10] (3) + mov r11,[r11+0x10] (4)
#
# Trampoline (hand-encoded, 27 bytes):
#   off 0   4C 89 15 <i32>   mov [rip+disp -> slot], r10   (7)
#   off 7   <stolen 7>        relocated original            (7)
#   off 14  E9 <i32>          jmp hook_addr + 7             (5)
#   off 19  <8 bytes>         slot (dq 0)
AOB_PLAYER = "4D 8B 1A 4D 8B 5B ?? 49 8B CA 8B 55 ?? 4D 8B C1"
# the 9 bytes the detour does NOT steal (used to re-find a site already hooked)
AOB_TAIL = "49 8B CA 8B 55 ?? 4D 8B C1"
ORIGINAL_BYTES = bytes([0x4D, 0x8B, 0x1A, 0x4D, 0x8B, 0x5B, 0x10])  # the stolen 7
TRAMP_SIG = b"\x4c\x89\x15"            # our trampoline starts: mov [rip+disp],r10
STOLEN_LEN = 7
SLOT_OFF = 7 + STOLEN_LEN + 5          # = 19
OFF_XYZ = 0x98                          # x,y,z contiguous f64 in the player struct
OFF_HEADING = 0xB0                      # facing angle (f64 radians = atan2(dy,dx));
                                        # calibrated live, R=0.945 vs movement dir


def _build_trampoline(tramp_addr: int, hook_addr: int, original: bytes) -> bytes:
    off_orig = 7
    off_jmp = off_orig + len(original)
    off_slot = off_jmp + 5
    disp = (tramp_addr + off_slot) - (tramp_addr + 7)   # rip is at next insn
    mov = b"\x4C\x89\x15" + struct.pack("<i", disp)
    rel = (hook_addr + len(original)) - (tramp_addr + off_jmp + 5)
    jmp = b"\xE9" + struct.pack("<i", rel)
    body = mov + bytes(original) + jmp + struct.pack("<Q", 0)
    assert len(body) == off_slot + 8
    return body


class HookLocator:
    """Drop-in replacement for PlayerLocator using the read-only-effect hook.

    Same surface (`locate`, `address`, `read_xyz`) plus `disable()`."""

    POLL_TIMEOUT = 4.0      # seconds to wait for the hook to fire (player spawned)

    def __init__(self, proc: Proc, hl: Hl | None = None):
        self.proc = proc
        self.hl = hl or Hl(proc)
        self._hook: inject.Hook | None = None

    @property
    def active(self) -> bool:
        return self._hook is not None and self._hook.active

    def _slot_addr(self) -> int | None:
        if self._hook is None or self._hook.tramp_addr is None:
            return None
        return self._hook.tramp_addr + SLOT_OFF

    def pbase(self) -> int | None:
        """The live player pointer from the slot, or None if not fired yet."""
        slot = self._slot_addr()
        if slot is None:
            return None
        raw = self.proc.try_read(slot, 8)
        if raw is None:
            return None
        ptr = struct.unpack("<Q", raw)[0]
        return ptr or None

    @property
    def address(self) -> int | None:
        return self.pbase()

    def _resolve_site(self) -> tuple[int, int | None]:
        """Return (site, existing_tramp). existing_tramp is None for a clean
        site (install fresh); non-None means a prior session's hook is still
        live and we adopt it (read its slot, clean it up on detach)."""
        try:
            return find_unique(self.proc, AOB_PLAYER, executable=True), None
        except ProcError:
            pass
        # the original bytes are gone — the site may already carry OUR detour
        # from a session that wasn't detached cleanly (e.g. hard-killed).
        for t in scan(self.proc, AOB_TAIL, executable=True, limit=12):
            site = t - STOLEN_LEN
            head = self.proc.try_read(site, 5)
            if not head or head[0] != 0xE9:
                continue
            tramp = site + 5 + struct.unpack("<i", head[1:5])[0]
            if self.proc.try_read(tramp, 3) == TRAMP_SIG:
                return site, tramp        # adopt the leftover hook
        raise ProcError(
            "player AOB not found and no recoverable hook present — the game was "
            "patched, or Farever.CT is hooked here (unload it and retry).")

    def locate(self) -> int | None:
        """Install (or adopt) the hook and wait for it to capture the player."""
        if not self.proc.has_scan:
            raise ProcError("hook locate needs the farever_native backend")
        if not self.active:
            site, tramp = self._resolve_site()
            hook = inject.Hook(self.proc, site, STOLEN_LEN, _build_trampoline)
            if tramp is not None:         # adopt a still-live leftover hook
                hook.original = ORIGINAL_BYTES
                hook.tramp_addr = tramp
                hook.active = True
            else:
                hook.enable()
            self._hook = hook
        # poll the slot until the hooked method runs (player in-world)
        deadline = time.monotonic() + self.POLL_TIMEOUT
        while time.monotonic() < deadline:
            pb = self.pbase()
            if pb is not None:
                return pb
            time.sleep(0.05)
        return self.pbase()

    def relocate(self) -> int | None:
        return self.pbase()

    def read_xyz(self) -> tuple[float, float, float] | None:
        base = self.pbase()
        if base is None:
            return None
        raw = self.proc.try_read(base + OFF_XYZ, 24)
        if raw is None:
            return None
        return struct.unpack("<ddd", raw)

    def read_heading(self) -> float | None:
        """Player facing in radians (atan2 convention: 0 = +x, CCW toward +y).
        Updates while turning/moving; holds when still."""
        base = self.pbase()
        if base is None:
            return None
        raw = self.proc.try_read(base + OFF_HEADING, 8)
        if raw is None:
            return None
        return struct.unpack("<d", raw)[0]

    def disable(self) -> None:
        if self._hook is not None:
            try:
                self._hook.disable()
            except ProcError:
                pass
            self._hook = None


# ---------------------------------------------------------------------------
#  CameraLocator — captures the Heaps camera via the CT 'fov' site to read the
#  free-look yaw (mouse orbit), which is NOT in the player struct.
# ---------------------------------------------------------------------------
# fov site: movsd xmm0,[r8+disp32] ; ... ; mov r10,[r8]   (r8 = h3d.Camera).
# We steal the 9-byte movsd, capture r8 into our slot, re-run it, jump back.
#   AOB_FOV: F2 49 0F 10 80 ?? ?? ?? ?? F2 48 0F 11 45 ?? 4D 8B 10
# Trampoline (hand-encoded):
#   off 0   4C 89 05 <i32>   mov [rip+disp -> slot], r8   (7)
#   off 7   <stolen 9>        relocated movsd (r8-relative, safe)
#   off 16  E9 <i32>          jmp hook+9                   (5)
#   off 21  <8 bytes>         slot (camera ptr)
AOB_FOV = "F2 49 0F 10 80 ?? ?? ?? ?? F2 48 0F 11 45 ?? 4D 8B 10"
AOB_FOV_TAIL = "F2 48 0F 11 45 ?? 4D 8B 10"   # the 9 bytes the detour leaves intact
CAM_STOLEN = 9
CAM_SLOT_OFF = 7 + CAM_STOLEN + 5             # = 21
CAM_TRAMP_SIG = b"\x4c\x89\x05"               # mov [rip+disp], r8
OFF_CAM_YAW = 0xD0                            # f64 radians (-pi..pi), live-calibrated


def _build_cam_trampoline(tramp_addr: int, hook_addr: int, original: bytes) -> bytes:
    off_jmp = 7 + len(original)
    off_slot = off_jmp + 5
    disp = (tramp_addr + off_slot) - (tramp_addr + 7)
    mov = b"\x4C\x89\x05" + struct.pack("<i", disp)
    rel = (hook_addr + len(original)) - (tramp_addr + off_jmp + 5)
    jmp = b"\xE9" + struct.pack("<i", rel)
    body = mov + bytes(original) + jmp + struct.pack("<Q", 0)
    assert len(body) == off_slot + 8
    return body


class CameraLocator:
    """Read-only-effect hook on the fov site → live camera object → free-look
    yaw. Best-effort: if it can't install, callers fall back (no camera yaw)."""

    POLL_TIMEOUT = 3.0

    def __init__(self, proc: Proc):
        self.proc = proc
        self._hook: inject.Hook | None = None

    @property
    def active(self) -> bool:
        return self._hook is not None and self._hook.active

    def _slot(self) -> int | None:
        if self._hook is None or self._hook.tramp_addr is None:
            return None
        return self._hook.tramp_addr + CAM_SLOT_OFF

    def _cam(self) -> int | None:
        s = self._slot()
        if s is None:
            return None
        raw = self.proc.try_read(s, 8)
        if raw is None:
            return None
        return struct.unpack("<Q", raw)[0] or None

    def _resolve_site(self) -> tuple[int, int | None, bytes | None]:
        """(site, existing_tramp, original_bytes). existing_tramp None => fresh."""
        try:
            return find_unique(self.proc, AOB_FOV, executable=True), None, None
        except ProcError:
            pass
        for t in scan(self.proc, AOB_FOV_TAIL, executable=True, limit=12):
            site = t - CAM_STOLEN
            head = self.proc.try_read(site, 5)
            if not head or head[0] != 0xE9:
                continue
            tramp = site + 5 + struct.unpack("<i", head[1:5])[0]
            if self.proc.try_read(tramp, 3) == CAM_TRAMP_SIG:
                orig = self.proc.try_read(tramp + 7, CAM_STOLEN)   # stored in tramp
                return site, tramp, orig
        raise ProcError("fov AOB not found / not recoverable")

    def enable(self) -> bool:
        if self.active or not self.proc.has_scan:
            return self.active
        try:
            site, tramp, orig = self._resolve_site()
            hook = inject.Hook(self.proc, site, CAM_STOLEN, _build_cam_trampoline)
            if tramp is not None and orig is not None:
                hook.original = orig
                hook.tramp_addr = tramp
                hook.active = True
            else:
                hook.enable()
            self._hook = hook
        except (ProcError, Exception):
            self._hook = None
            return False
        # wait for it to fire
        deadline = time.monotonic() + self.POLL_TIMEOUT
        while time.monotonic() < deadline:
            if self._cam() is not None:
                return True
            time.sleep(0.05)
        return True

    def yaw(self) -> float | None:
        cam = self._cam()
        if cam is None:
            return None
        raw = self.proc.try_read(cam + OFF_CAM_YAW, 8)
        if raw is None:
            return None
        return struct.unpack("<d", raw)[0]

    def disable(self) -> None:
        if self._hook is not None:
            try:
                self._hook.disable()
            except ProcError:
                pass
            self._hook = None
