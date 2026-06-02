"""Updater pure logic (headless): version parsing/compare, the check() decision
over a faked API, and exe extraction from a zip. No network, no real exe.
"""
import zipfile

from farever_companion.core import updater
from farever_companion.core.updater import parse_version, is_newer, UpdateInfo


# --- version parsing / comparison -----------------------------------------
def test_parse_version_strips_v_and_suffix():
    assert parse_version("v0.1.2") == (0, 1, 2)
    assert parse_version("0.1.2-beta3") == (0, 1, 2, 3)
    assert parse_version("") == (0,)
    assert parse_version("garbage") == (0,)


def test_is_newer():
    assert is_newer("0.1.2", "0.1.1") is True
    assert is_newer("0.2.0", "0.1.9") is True
    assert is_newer("1.0", "0.9.9") is True
    assert is_newer("0.1.1", "0.1.1") is False      # equal is not newer
    assert is_newer("0.1.0", "0.1.1") is False      # older
    assert is_newer("v0.1.2", "0.1.1") is True      # tolerates a leading v


# --- check() over a fake API ----------------------------------------------
class _FakeAPI:
    def __init__(self, payload):
        self._payload = payload

    def latest_release(self):
        return self._payload


def _bump_major(v: str) -> str:
    parts = list(parse_version(v))
    parts[0] += 1
    return ".".join(str(p) for p in parts)


def test_check_returns_info_when_newer():
    bumped = _bump_major(updater.current_version())
    info = updater.check(_FakeAPI({
        "ok": True, "version": bumped, "url": "https://x/y.zip",
        "notes": "n", "sha256": "abc", "size": 10,
    }))
    assert isinstance(info, UpdateInfo)
    assert info.version == bumped
    assert info.url == "https://x/y.zip"
    assert info.sha256 == "abc" and info.size == 10


def test_check_none_when_same_version():
    assert updater.check(_FakeAPI({
        "ok": True, "version": updater.current_version(), "url": "https://x/y.zip",
    })) is None


def test_check_none_on_error_payload():
    assert updater.check(_FakeAPI({"ok": False, "error": "network"})) is None
    assert updater.check(_FakeAPI({"ok": True, "version": "9.9.9", "url": ""})) is None
    assert updater.check(_FakeAPI(None)) is None


def test_check_swallows_api_exception():
    class Boom:
        def latest_release(self):
            raise RuntimeError("boom")
    assert updater.check(Boom()) is None


# --- staging the downloaded asset (bare exe OR zip) -----------------------
def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_stage_bare_exe(tmp_path):
    # The current release convention: a bare PE exe (starts with "MZ").
    dl = tmp_path / "FareverPal.exe.download"
    dl.write_bytes(b"MZ" + b"\x00" * 64)
    dest = tmp_path / "FareverPal.exe.new"
    updater._stage_payload(dl, "FareverPal.exe", dest)
    assert dest.read_bytes().startswith(b"MZ")
    assert not dl.exists()                         # bare exe is moved, not copied


def test_stage_zip_prefers_named(tmp_path):
    dl = tmp_path / "asset.download"
    _write_zip(dl, {"README.txt": "hi", "FareverPal.exe": b"MZ-named"})
    dest = tmp_path / "FareverPal.exe.new"
    updater._stage_payload(dl, "FareverPal.exe", dest)
    assert dest.read_bytes() == b"MZ-named"


def test_stage_zip_falls_back_to_any_exe(tmp_path):
    dl = tmp_path / "asset.download"
    _write_zip(dl, {"Renamed.exe": b"MZ2"})
    dest = tmp_path / "out.new"
    updater._stage_payload(dl, "FareverPal.exe", dest)
    assert dest.read_bytes() == b"MZ2"


def test_stage_zip_raises_when_no_exe(tmp_path):
    dl = tmp_path / "asset.download"
    _write_zip(dl, {"data.bin": b"x"})
    try:
        updater._stage_payload(dl, "FareverPal.exe", tmp_path / "o.new")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_stage_rejects_unknown_payload(tmp_path):
    dl = tmp_path / "asset.download"
    dl.write_bytes(b"not an exe or zip")
    try:
        updater._stage_payload(dl, "FareverPal.exe", tmp_path / "o.new")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass
