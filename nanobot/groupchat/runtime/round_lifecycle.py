"""RoundLifecycle — the single owner of group-chat round phase state.

Replaces phase inference from scattered flag conjunctions
(``not engine._running``, ``leader_end_event.is_set()``,
``all(t.done() for t in tasks)``) that previously lived at several call
sites and could disagree with each other (e.g. ``_inject_retry`` flipping
``engine._running`` back True while teardown was already in flight).

``RoundLifecycle`` is the source of truth for ROUND state. Its transitions
still set the legacy ``leader_end_event`` (sentinel / auto-wait polling
reads it), but they no longer write ``engine._running``: that flag is a
pure SESSION-level signal owned by the engine start/stop paths, and the
round's verdict on the session leaves via ``session_should_stop``, which
``broadcast_round`` returns to ``run_loop`` as ``RoundResult``.

The class is deliberately synchronous (no ``await`` anywhere): every
transition is atomic under asyncio's cooperative scheduling, so no reader
can observe a half-applied transition.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

from nanobot.groupchat.runtime.events import get_bus

# Reasons after which the whole session loop should exit once the round
# returns — surfaced as RoundResult.session_should_stop. "converged"
# (leaderless quiet group) keeps the session alive.
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
        Engine reference; used only to enrich event-bus emissions. Never
        mutated — ``engine._running`` is session-level state owned by the
        engine start/stop paths.
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
    ) -> None:
        """Request round teardown (end_discussion / leader crash / convergence / timeout).

        Idempotent: re-calling while WINDING_DOWN only updates the reason.
        No effect once ENDED. ``leader_exempt=True`` keeps the leader's
        cycle loop alive (leader still composing its synthesis — replaces
        the local ``_leader_ended_discussion`` flag dance). The session-level
        verdict travels out via ``session_should_stop`` (returned by
        broadcast_round); ``engine._running`` is never written here.
        """
        if self._phase is RoundPhase.ENDED:
            return
        self._phase = RoundPhase.WINDING_DOWN
        self._reason = reason
        self._leader_exempt = leader_exempt
        if self._leader_end_event is not None:
            self._leader_end_event.set()
        get_bus().emit_nowait(
            "round:winding_down",
            engine=self._engine, reason=reason, leader_exempt=leader_exempt,
        )

    def reopen(self, reason: str = "synthesis_retry") -> None:
        """Return WINDING_DOWN → ACTIVE (leader synthesis retry).

        Used by the leader synthesis-retry path (``_inject_retry``). The
        leader_end_event is NOT cleared: the sentinel may already have
        observed it, and un-setting a latched event mid-teardown is exactly
        the race this class exists to avoid. Known limitation (documented,
        unchanged from legacy): the grace-period straggler-cancel may still
        race a reopened leader.
        """
        if self._phase is not RoundPhase.WINDING_DOWN:
            return
        self._phase = RoundPhase.ACTIVE
        self._reason = ""
        self._leader_exempt = False
        get_bus().emit_nowait("round:reopened", engine=self._engine, reason=reason)

    def mark_ended(self) -> None:
        """Round fully torn down. Terminal — no transition leaves ENDED."""
        self._phase = RoundPhase.ENDED
        self._leader_exempt = False
        get_bus().emit_nowait("round:ended", engine=self._engine)

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

        Read by ``broadcast_round`` AFTER teardown (the reason survives
        ``mark_ended()``) and returned to run_loop as
        ``RoundResult.session_should_stop``. Legacy parity: leader
        end_discussion, leader crash and global timeout end the session;
        leaderless convergence does not (session waits for next message).
        """
        return self._reason in _SESSION_STOP_REASONS
