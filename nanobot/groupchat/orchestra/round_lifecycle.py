"""RoundLifecycle — the single owner of group-chat round phase state.

Replaces phase inference from scattered flag conjunctions
(``not engine._running``, ``leader_end_event.is_set()``,
``all(t.done() for t in tasks)``) that previously lived at several call
sites and could disagree with each other (e.g. ``_inject_retry`` flipping
``engine._running`` back True while teardown was already in flight).

Strangler-fig migration: ``RoundLifecycle`` is the source of truth, and its
transitions also flip the legacy signals (``leader_end_event``,
``engine._running``) so readers not yet migrated keep working unchanged.

The class is deliberately synchronous (no ``await`` anywhere): every
transition is atomic under asyncio's cooperative scheduling, so no reader
can observe a half-applied transition.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

# Reasons after which the whole session loop should exit once the round
# returns (parity with the legacy writers that set engine._running=False).
# "converged" (leaderless quiet group) keeps the session alive.
_SESSION_STOP_REASONS = frozenset({
    "leader_end_discussion",
    "leader_crash",
    "global_timeout",
})


class RoundPhase(Enum):
    ACTIVE = "active"            # agents running; user interjections accepted
    WINDING_DOWN = "winding_down"  # end requested; teardown may still run
    ENDED = "ended"              # round fully torn down (broadcast_round returned)


class RoundLifecycle:
    """Per-round phase state machine.

    Parameters
    ----------
    leader_end_event:
        Legacy notification event; ``mark_winding_down`` sets it so the
        existing sentinel / auto-wait polling keeps working.
    engine:
        Legacy engine reference; ``mark_winding_down(flip_running=True)``
        sets ``engine._running = False`` for parity with un-migrated readers.
    """

    def __init__(
        self,
        *,
        leader_end_event: asyncio.Event | None = None,
        engine: Any = None,
    ) -> None:
        self._phase = RoundPhase.ACTIVE
        self._reason: str = ""
        self._leader_exempt = False
        self._leader_end_event = leader_end_event
        self._engine = engine

    # ── Transitions ────────────────────────────────────────────────────────

    def mark_winding_down(
        self,
        reason: str,
        *,
        leader_exempt: bool = False,
        flip_running: bool = False,
    ) -> None:
        """Request round teardown (end_discussion / leader crash / convergence / timeout).

        Idempotent: re-calling while WINDING_DOWN only updates the reason.
        No effect once ENDED. ``leader_exempt=True`` keeps the leader's
        cycle loop alive (leader still composing its synthesis — replaces
        the local ``_leader_ended_discussion`` flag dance).
        """
        if self._phase is RoundPhase.ENDED:
            return
        self._phase = RoundPhase.WINDING_DOWN
        self._reason = reason
        self._leader_exempt = leader_exempt
        if self._leader_end_event is not None:
            self._leader_end_event.set()
        if flip_running and self._engine is not None:
            self._engine._running = False

    def reopen(self, reason: str = "synthesis_retry") -> None:
        """Return WINDING_DOWN → ACTIVE (leader synthesis retry).

        Mirrors the legacy ``_inject_retry`` behaviour of flipping
        ``engine._running`` back True. The leader_end_event is NOT cleared:
        the sentinel may already have observed it, and un-setting a latched
        event mid-teardown is exactly the race this class exists to avoid.
        Known limitation (documented, unchanged from legacy): the grace-period
        straggler-cancel may still race a reopened leader.
        """
        if self._phase is not RoundPhase.WINDING_DOWN:
            return
        self._phase = RoundPhase.ACTIVE
        self._reason = ""
        self._leader_exempt = False
        if self._engine is not None:
            self._engine._running = True

    def mark_ended(self) -> None:
        """Round fully torn down. Terminal — no transition leaves ENDED."""
        self._phase = RoundPhase.ENDED
        self._leader_exempt = False

    # ── Queries (replace scattered flag conjunctions) ──────────────────────

    @property
    def phase(self) -> RoundPhase:
        return self._phase

    @property
    def reason(self) -> str:
        return self._reason

    def accepts_interjection(self) -> bool:
        """True while a mid-round user message can still be delivered."""
        return self._phase is RoundPhase.ACTIVE

    def agents_should_exit(self, *, is_leader: bool) -> bool:
        """Per-agent cycle-loop exit decision.

        Everyone exits once winding down, except an exempt leader that is
        still composing its end-of-discussion synthesis.
        """
        if self._phase is RoundPhase.ACTIVE:
            return False
        if self._phase is RoundPhase.ENDED:
            return True
        return not (self._leader_exempt and is_leader)

    def wait_should_exit(self) -> bool:
        """Auto-wait should stop parking once the round is ending."""
        return self._phase is not RoundPhase.ACTIVE

    @property
    def session_should_stop(self) -> bool:
        """Whether run_loop should exit the session after this round.

        Legacy parity: leader end_discussion, leader crash and global
        timeout all flipped ``engine._running`` off (session exits);
        leaderless convergence did not (session waits for next message).
        """
        return self._reason in _SESSION_STOP_REASONS
