"""Read-only guarantee: the memory-WRITE path is structurally absent.

Motivating incident: a build (v0.1.3) shipped the memory-WRITE code hook as the
DEFAULT because it was built from uncommitted local edits. Rather than ship that
write path gated behind an opt-in, the companion now **removes it entirely** --
there is no code-hook, no injector, and no write primitive in the tree at all.
The tool can only ever read the game.

These tests lock that contract in place so a future edit can't quietly
reintroduce a write path:

  (a) the inject module (Injector / Hook) does NOT exist -- importing it fails.
  (b) core.player exposes the pure-read PlayerLocator and NOT a write HookLocator.
  (c) config exposes no hook gate (hook_locate_enabled is gone with the hook).
  (d) model.py selects the pure-read locator and references no hook at all.
  (e) the strong backstop: NO write primitive (WriteProcessMemory,
      PROCESS_VM_WRITE, the hook locator, or the FAREVER_ENABLE_HOOK gate)
      appears anywhere in the farever_companion package source.
  (f) the native Rust reader (native/src) exposes no write primitive either:
      no WriteProcessMemory / VirtualAllocEx / VirtualProtectEx / PROCESS_VM_WRITE.
      The handle is opened read-only, so the shipped binary cannot write at all.

Everything here is pure source/text inspection plus a couple of guarded imports,
so it runs headless in CI with no live game, no Qt widget, and no native build.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

from farever_companion import config

REPO_DIR = pathlib.Path(__file__).resolve().parents[1]
PKG_DIR = REPO_DIR / "farever_companion"
NATIVE_SRC = REPO_DIR / "native" / "src"

# Tokens that would signal a memory-WRITE capability creeping back in. None of
# these may appear anywhere in the package source.
FORBIDDEN_TOKENS = (
    "WriteProcessMemory",   # the Win32 write primitive
    "PROCESS_VM_WRITE",     # the write access right on the process handle
    "HookLocator",          # the write-hook player locator
    "hook_locate_enabled",  # the old opt-in gate (removed with the hook)
    "FAREVER_ENABLE_HOOK",  # the old gate's env var
)


def _py_sources():
    for path in PKG_DIR.rglob("*.py"):
        yield path, path.read_text(encoding="utf-8", errors="replace")


# --- (a) the write/inject module is gone -----------------------------------
def test_inject_module_does_not_exist():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("farever_companion.core.inject")


# --- (b) player exposes the pure-read locator only -------------------------
def test_player_has_pure_read_locator_only():
    try:
        from farever_companion.core import player
    except (OSError, ImportError) as e:  # pragma: no cover - non-Windows hosts
        pytest.skip(f"core.player unavailable on this platform: {e}")
    assert hasattr(player, "PlayerLocator")
    assert not hasattr(player, "HookLocator")


# --- (c) the hook gate is gone from config ---------------------------------
def test_config_has_no_hook_gate():
    assert not hasattr(config, "hook_locate_enabled")


# --- (d) model selects the pure-read locator, references no hook ------------
def test_model_source_uses_pure_read_locator_only():
    src = (PKG_DIR / "core" / "model.py").read_text(encoding="utf-8")
    assert "PlayerLocator" in src
    assert "HookLocator" not in src
    assert "hook_locate_enabled" not in src


# --- (e) strong backstop: no write primitive anywhere in the package -------
@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_no_write_primitive_in_package(token):
    offenders = [str(p.relative_to(PKG_DIR)) for p, txt in _py_sources()
                 if token in txt]
    assert not offenders, (
        f"forbidden write-path token {token!r} found in: {offenders}. "
        "The companion is read-only; the memory-write hook was removed and must "
        "not be reintroduced.")


# --- (f) the native Rust reader has no write primitive either --------------
# Win32 write APIs that must never appear in the read-only reader. A real call
# requires `use`-importing the symbol, so a substring match on the source is a
# reliable guard (and is robust to formatting).
RUST_FORBIDDEN_TOKENS = (
    "WriteProcessMemory",
    "VirtualAllocEx",
    "VirtualProtectEx",
    "PROCESS_VM_WRITE",
    "PROCESS_VM_OPERATION",
)


@pytest.mark.parametrize("token", RUST_FORBIDDEN_TOKENS)
def test_no_write_primitive_in_native(token):
    if not NATIVE_SRC.is_dir():  # pragma: no cover - native sources not present
        pytest.skip("native/src not present in this checkout")
    offenders = [str(p.relative_to(NATIVE_SRC)) for p in NATIVE_SRC.rglob("*.rs")
                 if token in p.read_text(encoding="utf-8", errors="replace")]
    assert not offenders, (
        f"forbidden Win32 write API {token!r} found in native source: {offenders}. "
        "The Rust reader opens the process read-only and must expose no write path.")
