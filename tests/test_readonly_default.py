"""Read-only-by-default regression guard (headless; no live game required).

Motivating incident: a release (v0.1.3) shipped the memory-WRITE code hook as the
DEFAULT because it was built from uncommitted local edits that deleted the
read-only gate. These tests lock the contract in place so it can't regress
silently:

  (a) env unset  -> config.hook_locate_enabled() is False (read-only default).
  (b) env unset  -> the locator the model selects is the pure-read PlayerLocator,
      NOT the write-hook HookLocator.
  (c) backstop   -> env unset -> the injector / Hook write path RAISES a loud
      guard error (no silent write), even with a fake process.
  (d) env="1"    -> the gate reports enabled (the opt-in path still works).

Everything here is pure Python: it builds tiny fakes for the process so no real
game / handle is needed, instantiates no Qt widget, and runs headless in CI.
"""
from __future__ import annotations

import pytest

from farever_companion import config

# core.player imports core.inject, which loads kernel32 via ctypes.WinDLL at
# import time (Windows-only). In CI (windows-latest) this imports fine and the
# whole suite runs; off-Windows we skip the module cleanly instead of erroring
# at collection. config (above) is pure stdlib and always imports, so the gate
# tests for it would still run even without this guard.
try:
    from farever_companion.core.player import PlayerLocator, HookLocator
except (OSError, ImportError) as _e:  # pragma: no cover - non-Windows dev hosts
    pytest.skip(f"core.player unavailable on this platform: {_e}",
                allow_module_level=True)

HOOK_ENV = "FAREVER_ENABLE_HOOK"


class _FakeProc:
    """Minimal stand-in for core.proc.Proc.

    The locator constructors (PlayerLocator / HookLocator) and inject.Injector
    only need `pid` and (for Hl construction) an object they can store; they do
    NO I/O at construction time, so this never touches a real process.
    """

    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.kind = "native"

    @property
    def has_scan(self) -> bool:
        return True


def _select_locator(proc):
    """Mirror LiveModel.__init__'s locator selection EXACTLY (core/model.py):

        self._allow_hook = hook_locate_enabled()
        self.locator = (HookLocator(...) if self._allow_hook
                        else PlayerLocator(...))

    Constructed with the real classes + the real gate so this stays in lockstep
    with the production selection without instantiating the heavy LiveModel
    (which loads chest data, a DPS meter, etc.). The point under test is the
    gate, not those collaborators.
    """
    if config.hook_locate_enabled():
        return HookLocator(proc)
    return PlayerLocator(proc)


# --- (a) the gate is OFF by default ----------------------------------------
def test_gate_is_off_when_env_unset(monkeypatch):
    monkeypatch.delenv(HOOK_ENV, raising=False)
    assert config.hook_locate_enabled() is False


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "OFF", "False"])
def test_gate_is_off_for_falsey_values(monkeypatch, val):
    monkeypatch.setenv(HOOK_ENV, val)
    assert config.hook_locate_enabled() is False


# --- (b) default selection is the pure-read locator ------------------------
def test_default_locator_is_pure_read(monkeypatch):
    monkeypatch.delenv(HOOK_ENV, raising=False)
    locator = _select_locator(_FakeProc())
    assert isinstance(locator, PlayerLocator)
    assert not isinstance(locator, HookLocator)


def test_locator_is_hook_when_enabled(monkeypatch):
    monkeypatch.setenv(HOOK_ENV, "1")
    locator = _select_locator(_FakeProc())
    assert isinstance(locator, HookLocator)


# --- (c) backstop: the write path REFUSES to write when the gate is off ----
def _import_inject_or_skip():
    """Import core.inject, skipping if the platform lacks the Win32 DLLs.

    core.inject loads kernel32 via ctypes.WinDLL at import time, which only
    exists on Windows. In CI (windows-latest) it imports fine and these asserts
    run; off-Windows we skip rather than fail collection.
    """
    try:
        from farever_companion.core import inject
    except (OSError, ImportError) as e:  # non-Windows: no kernel32
        pytest.skip(f"core.inject unavailable on this platform: {e}")
    return inject


def test_injector_refuses_to_open_write_handle_by_default(monkeypatch):
    monkeypatch.delenv(HOOK_ENV, raising=False)
    inject = _import_inject_or_skip()
    # Opening the PROCESS_VM_WRITE handle must raise BEFORE any OpenProcess call.
    with pytest.raises(RuntimeError, match="read-only by default"):
        inject.Injector(_FakeProc().pid)


def test_hook_enable_refuses_to_write_by_default(monkeypatch):
    monkeypatch.delenv(HOOK_ENV, raising=False)
    inject = _import_inject_or_skip()
    # Hook.__init__ builds an Injector, so even constructing a Hook is gated.
    with pytest.raises(RuntimeError, match="read-only by default"):
        inject.Hook(_FakeProc(), 0x1000, 7, lambda t, h, o: b"")


def test_hooklocator_locate_refuses_to_write_by_default(monkeypatch):
    """End-to-end backstop through the model's locator: with the gate off the
    HookLocator's write path must raise rather than silently inject (we never
    reach a real OpenProcess / WriteProcessMemory)."""
    monkeypatch.delenv(HOOK_ENV, raising=False)
    _import_inject_or_skip()  # ensure inject is importable on this platform

    locator = HookLocator(_FakeProc())

    # Stub site resolution so we exercise the inject path without a real game:
    # _resolve_site() -> (site, None) means "install fresh", which calls
    # Hook(...) -> Injector(...) -> the read-only guard fires.
    locator._resolve_site = lambda: (0x1000, None)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="read-only by default"):
        locator.locate()


# --- (d) the opt-in path reports enabled -----------------------------------
@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
def test_gate_reports_enabled_when_opted_in(monkeypatch, val):
    monkeypatch.setenv(HOOK_ENV, val)
    assert config.hook_locate_enabled() is True


# --- model.py's committed gate is intact (cheap source-level backstop) ------
def test_model_source_keeps_the_gate():
    """The committed LiveModel must still select on the gate. Catches a future
    edit that hard-codes HookLocator (the exact v0.1.3 regression)."""
    import farever_companion.core.model as model

    src = __import__("inspect").getsource(model)
    assert "hook_locate_enabled" in src
    assert "PlayerLocator" in src and "HookLocator" in src
