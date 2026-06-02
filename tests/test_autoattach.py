"""Auto-attach state machine (headless — no game, no Qt).

Covers the decision table in core/autoattach.py: attach when the game opens,
relocate until the player loads in, detach on close/restart, and honour the
enabled/busy guards.
"""
from farever_companion.core.autoattach import AutoAttach, Act


def _a():
    return AutoAttach()


# --- attaching -------------------------------------------------------------
def test_not_running_is_noop():
    assert _a().decide(enabled=True, running_pid=None, attached_pid=None,
                       located=False, busy=False) is Act.NONE


def test_running_and_detached_attaches():
    assert _a().decide(enabled=True, running_pid=1234, attached_pid=None,
                       located=False, busy=False) is Act.ATTACH


def test_disabled_never_attaches():
    assert _a().decide(enabled=False, running_pid=1234, attached_pid=None,
                       located=False, busy=False) is Act.NONE


def test_busy_suppresses_attach():
    assert _a().decide(enabled=True, running_pid=1234, attached_pid=None,
                       located=False, busy=True) is Act.NONE


# --- relocate / steady state ----------------------------------------------
def test_attached_not_located_relocates():
    assert _a().decide(enabled=True, running_pid=1234, attached_pid=1234,
                       located=False, busy=False) is Act.RELOCATE


def test_attached_not_located_but_busy_is_noop():
    assert _a().decide(enabled=True, running_pid=1234, attached_pid=1234,
                       located=False, busy=True) is Act.NONE


def test_attached_and_located_is_noop():
    assert _a().decide(enabled=True, running_pid=1234, attached_pid=1234,
                       located=True, busy=False) is Act.NONE


# --- detaching -------------------------------------------------------------
def test_game_closed_detaches():
    assert _a().decide(enabled=True, running_pid=None, attached_pid=1234,
                       located=True, busy=False) is Act.DETACH


def test_game_restarted_new_pid_detaches():
    assert _a().decide(enabled=True, running_pid=5678, attached_pid=1234,
                       located=True, busy=False) is Act.DETACH


def test_detach_happens_even_when_disabled():
    # The hidden settings.json off-switch should still let go cleanly on close.
    assert _a().decide(enabled=False, running_pid=None, attached_pid=1234,
                       located=True, busy=False) is Act.DETACH


def test_restart_then_reattach():
    a = _a()
    # game closed while attached -> detach
    assert a.decide(enabled=True, running_pid=None, attached_pid=1234,
                    located=True, busy=False) is Act.DETACH
    # after the UI detaches, the relaunched game (new pid) -> attach again
    assert a.decide(enabled=True, running_pid=5678, attached_pid=None,
                    located=False, busy=False) is Act.ATTACH
