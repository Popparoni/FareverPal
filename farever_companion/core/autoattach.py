"""Auto-attach decision logic — a tiny pure state machine.

The UI polls (running pid, attached pid, located?, busy) on a timer and asks
this controller what to do. Keeping the *decision* here (no Qt, no I/O) means it
unit-tests without the game running; the UI just executes the returned action.

Attach/detach is fully automatic — there are no buttons or toggle in the UI — so
this is the whole policy: attach when the game opens, relocate until the player
is in-world, detach when it closes or restarts. It only ever returns one of the
existing read-only actions (attach / locate / detach); it adds no memory access.
"""
from __future__ import annotations

from enum import Enum


class Act(Enum):
    NONE = 0
    ATTACH = 1
    DETACH = 2
    RELOCATE = 3


class AutoAttach:
    """Decides attach/detach/relocate from the current observation. Stateless."""

    def decide(self, *, enabled: bool, running_pid: int | None,
               attached_pid: int | None, located: bool, busy: bool) -> Act:
        if attached_pid is not None:
            # Game closed, or restarted under a new pid -> let go cleanly.
            if running_pid is None or running_pid != attached_pid:
                return Act.DETACH
            # Attached to the live game but not yet in-world: keep trying to
            # locate the player until they load in (unless a locate is running).
            if not located and not busy:
                return Act.RELOCATE
            return Act.NONE
        # Not attached: attach as soon as the game is up (unless disabled/busy).
        if not enabled or busy or running_pid is None:
            return Act.NONE
        return Act.ATTACH
