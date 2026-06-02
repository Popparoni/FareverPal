"""In-app updater, checks GitHub Releases (via the website's /api/release.php
oracle) and self-replaces the one-file exe.

Flow: the desktop app asks farever-pals.com what the latest release is (the site
already knows the GitHub repo from its Download config), compares versions, and -
if newer, downloads the GitHub asset, verifies it, and swaps itself on disk.

Windows wrinkle: a running .exe is locked and can't be overwritten, but it *can*
be renamed. So we rename the live exe to `*.old`, move the new one into place,
relaunch, and delete the leftover `*.old` on the next start.

Everything here degrades cleanly: a missing network, an old Python (running from
source rather than a frozen exe), or a checksum mismatch all abort without
touching the installed app. The pure helpers (version parsing/compare) are
unit-tested; the file-swap needs a real frozen exe and is exercised manually.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .. import __version__


@dataclass(frozen=True)
class UpdateInfo:
    version: str            # "0.1.2"
    url: str                # GitHub asset (zip) download URL
    notes: str = ""         # release body (markdown)
    sha256: str | None = None
    size: int | None = None
    html_url: str = ""      # release page (browser fallback)


# --- pure version helpers (unit-tested) ------------------------------------
def parse_version(s: str) -> tuple[int, ...]:
    """"v0.1.2" / "0.1.2-beta" -> (0, 1, 2). Non-numeric junk is dropped; an
    empty/garbage string parses to (0,)."""
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums) or (0,)


def is_newer(latest: str, current: str = __version__) -> bool:
    """True if `latest` is a strictly higher version than `current`."""
    return parse_version(latest) > parse_version(current)


def current_version() -> str:
    return __version__


# --- frozen-exe helpers ----------------------------------------------------
def is_frozen() -> bool:
    """True when running as the packaged one-file exe (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def exe_path() -> Path:
    """The on-disk exe (for a one-file build this is the real launcher, not the
    _MEIPASS temp dir)."""
    return Path(sys.executable)


def _old_path(cur: Path) -> Path:
    return cur.with_name(cur.name + ".old")


def cleanup_old() -> None:
    """Delete the `*.old` left by a previous self-update. Best-effort; called on
    startup once the new exe is the running one (so the old file is unlocked)."""
    if not is_frozen():
        return
    old = _old_path(exe_path())
    try:
        if old.exists():
            old.unlink()
    except OSError:
        pass            # still locked / in use - next launch will get it


# --- check -----------------------------------------------------------------
def check(api) -> UpdateInfo | None:
    """Ask the web oracle for the latest release; return UpdateInfo if it's newer
    than us, else None. `api` is a FareverAPI. Never raises."""
    try:
        res = api.latest_release()
    except Exception:
        return None
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    ver = str(res.get("version") or "")
    url = str(res.get("url") or "")
    if not ver or not url or not is_newer(ver):
        return None
    size = res.get("size")
    return UpdateInfo(
        version=ver,
        url=url,
        notes=str(res.get("notes") or ""),
        sha256=(str(res["sha256"]) if res.get("sha256") else None),
        size=(int(size) if isinstance(size, int) else None),
        html_url=str(res.get("html_url") or ""),
    )


# --- download + stage ------------------------------------------------------
def download_and_stage(info: UpdateInfo, progress=None) -> Path:
    """Download the release asset, verify it, and produce the staged exe next to
    the running one as `<name>.new`; return that path. Raises on any failure
    (caller keeps the current install untouched).

    Handles both kinds of asset we publish: a **bare .exe** (the current
    convention) and a **.zip** containing the exe, detected by content, not by
    URL, so naming never matters. Streams to a temp file rather than buffering
    the whole ~300 MB payload in RAM. `progress(done, total)` is called during
    download (total may be 0 if unknown)."""
    if not is_frozen():
        raise RuntimeError("self-update only works from the packaged exe")

    cur = exe_path()
    tmp = cur.with_name(cur.name + ".download")
    new = cur.with_name(cur.name + ".new")
    req = urllib.request.Request(info.url, headers={"User-Agent": "FareverPal-Companion"})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            total = info.size or int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
        if tmp.stat().st_size == 0:
            raise RuntimeError("downloaded an empty file")
        if info.sha256 and h.hexdigest().lower() != info.sha256.lower():
            raise RuntimeError("checksum mismatch — refusing to install")
        if info.size and tmp.stat().st_size != info.size:
            raise RuntimeError("size mismatch — refusing to install")
        _stage_payload(tmp, cur.name, new)
        return new
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _stage_payload(downloaded: Path, exe_name: str, dest: Path) -> None:
    """Turn the downloaded asset into the staged exe at `dest`, handling either a
    bare PE executable or a zip that contains it (sniffed by magic bytes)."""
    with open(downloaded, "rb") as f:
        magic = f.read(4)
    if dest.exists():
        dest.unlink()
    if magic.startswith(b"PK\x03\x04"):            # zip archive
        with zipfile.ZipFile(downloaded) as zf:
            member = _pick_exe(zf.namelist(), exe_name)
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, 256 * 1024)
    elif magic.startswith(b"MZ"):                  # bare Windows executable
        os.replace(downloaded, dest)               # move (no 300 MB copy)
    else:
        raise RuntimeError("downloaded asset is neither an .exe nor a .zip")


def _pick_exe(members: list[str], exe_name: str) -> str:
    """Choose the exe member of a release zip: exact name first, else any .exe."""
    target = next((m for m in members if m.rsplit("/", 1)[-1].lower() == exe_name.lower()), None)
    if target is None:
        target = next((m for m in members if m.lower().endswith(".exe")), None)
    if target is None:
        raise RuntimeError("no .exe inside the release archive")
    return target


# --- apply (swap + relaunch) ----------------------------------------------
def apply_and_relaunch(new: Path) -> None:
    """Atomically move the staged exe over the running one and relaunch. The
    caller must quit the app immediately after this returns so the old process
    exits. Rolls back if the swap fails partway."""
    if not is_frozen():
        raise RuntimeError("self-update only works from the packaged exe")
    cur = exe_path()
    old = _old_path(cur)
    # Clear any stale .old so the rename below can't collide.
    try:
        if old.exists():
            old.unlink()
    except OSError:
        pass
    # Rename the live exe out of the way (allowed for a running image), then move
    # the new one into its place. If the second step fails, put the original back.
    os.replace(cur, old)
    try:
        os.replace(new, cur)
    except OSError:
        os.replace(old, cur)        # roll back - install stays on the old version
        raise

    flags = 0x00000008 | 0x00000200    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(cur)], cwd=str(cur.parent), close_fds=True, creationflags=flags)
