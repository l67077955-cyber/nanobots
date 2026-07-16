"""Architecture contract for the group-chat engine.

The seams future feature code may depend on. Protocol only —
implementations live elsewhere (``agent_runner.AgentRunner``,
``turn_stack.TurnStack``, ``cycle_controller.CycleController``).
New code must depend on these Protocols, not on ``MailboxHub`` /
``GroupChatEngine`` internals.

Conversation state is in ``engine.history`` (nanobot.core.history.History).

Conversation state port: ``nanobot.groupchat.context.conversation.ConversationContext`` (History façade). Not re-exported here to keep runtime ports focused on cancel/turn/cycle seams.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

# Re-export the decision types so callers depend on the port, not the
# concrete ``cycle_controller`` module. (Protocol can't own dataclasses.)
from nanobot.groupchat.runtime.cycle_controller import (
    CycleAction,
    CycleContext,
    CycleDecision,
)

__all__ = [
    "AgentRunner",
    "TurnStack",
    "CycleController",
    "CycleContext",
    "CycleDecision",
    "CycleAction",
]


@runtime_checkable
class AgentRunner(Protocol):
    """Per-agent runtime handle: owns the cancel signal + state queries.

    The single object future code touches to interrupt/cancel/inspect an
    agent at runtime. The key invariant: any blocking operation an agent
    performs (``wait``, an in-flight LLM call, future awaits) must race
    against ``interrupt_event`` — that is what makes cancellation reliable.

    State model (simplified per user insight): interrupt is a momentary event,
    not a dwell state. Agent only ever occupies busy (tool_loop in flight) or
    idle (no tool_loop). The ``interrupt_pending`` and ``is_waiting`` properties
    expose details of idle for observability, not separate state tiers.
    """

    name: str

    @property
    def interrupt_event(self) -> asyncio.Event:
        """Cooperative cancel signal blocking operations race against."""
        ...

    @property
    def state(self) -> str:
        """Derived state: busy | idle | done (three-tier model)."""
        ...

    @property
    def interrupt_pending(self) -> bool:
        """Whether an interrupt event is set (detail of idle)."""
        ...

    @property
    def is_waiting(self) -> bool:
        """Whether agent is blocked on mailbox.wait (detail of idle)."""
        ...

    def force_interrupt(self, sender: str = "用户", reason: str = "⏹ 已中断") -> bool:
        """High-priority cooperative interrupt (user/leader); bypasses rank."""
        ...

    def request_interrupt(self, sender: str) -> bool:
        """Rank-checked cooperative interrupt by a peer agent."""
        ...

    def cancel(self, reason: str = "⏹ 已停止") -> None:
        """Hard stop: cooperative interrupt + task.cancel() (bounded unwind)."""
        ...


@runtime_checkable
class TurnStack(Protocol):
    """Turn-level operations seam for a broadcast round (Step 2).

    The engine runs agents **concurrently** as asyncio tasks, not a sequential
    queue; this owns the turn-level ops that cut across all agents mid-round:
    user interjection, round cancellation, active-turn tracking. The per-agent
    cycle-loop decision extraction is a later, higher-risk step.
    """

    @property
    def active_agents(self) -> list[str]:
        ...

    async def interject(self, user_msg: str) -> bool:
        """Inject a user message into the live round. False if round winding down."""
        ...

    def cancel_all(self) -> int:
        """Cancel every in-flight agent task this round. Returns count cancelled."""
        ...


@runtime_checkable
class CycleController(Protocol):
    """Per-agent cycle-loop decision seam (Step 3b).

    Owns the *next-action selection* for ``_run_one``'s ``while True`` cycle
    loop — which ``continue``/``break`` branch to take given a state snapshot.
    The side-effecting bodies (tool_loop calls, message injection, display,
    state mutations) stay inline in ``broadcast._run_one`` until a later
    Step 3c; this port only plants the decision contract so the ~10 branch
    predicates are named, documented, and unit-testable in isolation.

    Composes with ``AgentRunner``: the runner mutates agent state
    (busy/idle/interrupt lifecycle), the controller reads a snapshot and says
    what to do next. Why five methods and not one ``decide()``: the post-
    ``tool_loop`` cascade has two unconditional bodies (history recording,
    display) sandwiched between the decision branches, so a single
    first-match action cannot express "run body X then keep evaluating". The
    five call sites split the cascade at those sandwiched bodies.

    See ``cycle_controller.py`` for the faithful precedence contract and
    ``docs/groupchat-coupling-fix.md`` (Step 3b).
    """

    def decide_cycle_gate(self, ctx: "CycleContext") -> "CycleDecision":
        """Pre-cycle: A max_cycles → forced synthesis+exit; B engine-stopped/
        discussion-ended → exit (with the leader-mid-synthesis exception)."""
        ...

    def decide_error_recovery(self, ctx: "CycleContext") -> "CycleDecision":
        """Post-tool_loop error/timeout gate. C1 retry *outcome* (success vs
        C2 placeholder) stays body-internal — only the gate-level action is
        resolved here. C3 is a fall-through (no continue)."""
        ...

    def decide_post_error_guard(self, ctx: "CycleContext") -> "CycleDecision":
        """D interrupt / E idle-cycle-1 / F no-text-after-tools / G
        leader-mgmt-no-text — first-match, then proceed to display."""
        ...

    def decide_leader_or_single_exit(self, ctx: "CycleContext") -> "CycleDecision":
        """H1/H2 leader-synthesis / I single-agent exit, else proceed to
        auto-wait."""
        ...

    def decide_after_wait(self, ctx: "CycleContext") -> "CycleDecision":
        """J1/J2/J3 wait-returned-None / K stopped-after-wait / L inject
        teammate message."""
        ...
