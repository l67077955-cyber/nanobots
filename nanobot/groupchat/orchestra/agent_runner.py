"""AgentRunner — per-agent runtime facade (Step 0.5 of the coupling refactor).

Owns the agent's *cancel signal* and exposes a stable runtime API. State is
NOT moved here yet: busy/idle/waiting still live on ``MailboxHub`` and the
async task still lives on ``engine._broadcast_tasks``. This class wraps those
so that NEW code depends on ``AgentRunner`` rather than reaching into
mailbox/engine internals.

Existing interrupt paths (``interrupt_busy_agents``, ``_try_interrupt``)
continue to work unchanged — they set the same ``asyncio.Event`` object this
runner wraps, so the two are always consistent.

Why this is the seed of the refactor: the 2026-07-08 2-min hang happened
because the interrupt was a side-channel ``Event`` that blocking operations
(``wait()``, in-flight LLM calls) did not poll. Centralising the cancel
signal behind this facade makes the contract explicit and is the migration
target for owning the full agent state machine.

See ``docs/groupchat-coupling-fix.md`` and ``nanobot/groupchat/orchestra/ports.py``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Optional

from loguru import logger

if TYPE_CHECKING:
    from nanobot.groupchat.orchestra.mailbox import MailboxHub


class AgentRunner:
    """Concrete ``ports.AgentRunner`` implementation (delegating, no state moved)."""

    def __init__(
        self,
        name: str,
        mailbox: "MailboxHub",
        task_getter: Callable[[], Optional[asyncio.Task]],
    ) -> None:
        self.name = name
        self._mailbox = mailbox
        self._task_getter = task_getter

    # ── Cancel signal ─────────────────────────────────────────────────────

    @property
    def interrupt_event(self) -> asyncio.Event:
        """The cooperative cancel signal blocking operations race against.

        Same object as ``mailbox.get_interrupt_event(name)`` — exposed as a
        property so callers depend on the runner, not mailbox. ``wait()`` and
        ``tool_loop``'s LLM-call race both poll this.
        """
        return self._mailbox.get_interrupt_event(self.name)

    @property
    def task(self) -> Optional[asyncio.Task]:
        """The agent's asyncio task, or None if not spawned this round."""
        return self._task_getter()

    # ── Derived state (migrates onto the runner in a later step) ──────────
    # Reads mailbox's _busy_agents / _waiting until those move here. This is
    # the single place that should touch those privates going forward.

    @property
    def is_busy(self) -> bool:
        return self.name in self._mailbox._busy_agents

    @property
    def state(self) -> str:
        """busy | idle | done (derived, not owned).

        Simplified per user insight: interrupt is a momentary event, not a
        dwell state — agent never "stays in interrupted". Likewise, waiting
        (blocked on mailbox.wait) is just a variant of idle (no tool_loop
        in flight). The three-state model aligns with the core invariant:
        busy = tool_loop racing interrupt, idle = no tool_loop, done = final.
        """
        t = self.task
        if t is None or t.done():
            return "done"
        if self.is_busy:
            return "busy"
        return "idle"

    @property
    def interrupt_pending(self) -> bool:
        """Whether an interrupt event is set (detail of idle, not a state tier)."""
        return self.interrupt_event.is_set()

    @property
    def is_waiting(self) -> bool:
        """Whether agent is blocked on mailbox.wait (detail of idle, not a state tier)."""
        return self.name in self._mailbox._waiting

    # ── Cycle state machine (Step 3) ──────────────────────────────────────
    # Owns the busy/idle transitions + interrupt lifecycle that _run_one used
    # to drive by reaching into mailbox.mark_busy / mark_idle / _interrupt_event
    # / _interrupt_counts inline. Now the runner is the single mutator of those,
    # matching the "agent state via concurrency" port contract. The cycle-loop
    # *decision* (which ~10 continue branch to take) is NOT extracted here —
    # that belongs on a separate CycleController and needs full verification.

    def begin_cycle(self) -> None:
        """Mark the agent busy (entering tool_loop)."""
        self._mailbox.mark_busy(self.name)

    def end_cycle(self) -> None:
        """Mark the agent idle (tool_loop exited: interrupt/stop/normal/error)."""
        self._mailbox.mark_idle(self.name)

    def acknowledge_interrupt(self) -> None:
        """Clear the interrupt event + reset the per-round interrupt counter.

        Called once the agent has reacted to an interrupt, so newer messages
        can re-interrupt in subsequent cycles (the freshness guarantee). This
        closes the interrupt lifecycle: set via ``force_interrupt`` /
        ``request_interrupt``, cleared here.
        """
        self.interrupt_event.clear()
        self._mailbox._interrupt_counts[self.name] = 0

    # ── Interrupt / cancel ────────────────────────────────────────────────

    def force_interrupt(
        self,
        sender: str = "用户",
        reason: str = "⏹ 已中断",
    ) -> bool:
        """High-priority cooperative interrupt (user/leader): set this agent's
        interrupt event, bypassing rank/quota checks. Mirrors
        ``MailboxHub.interrupt_busy_agents`` for a single agent.

        Returns True if the event was newly set, False if already set.
        """
        evt = self.interrupt_event
        if evt.is_set():
            return False
        # Record attribution so the UI ("⚡ X 被 Y 打断") still resolves the
        # sender after the queue is drained. Does not increment _interrupt_counts
        # (high-priority interrupts don't consume the per-round quota).
        self._mailbox._last_interrupt_sender[self.name] = sender
        evt.set()
        logger.info("AgentRunner: force_interrupt {} by {} ({})", self.name, sender, reason)
        return True

    def request_interrupt(self, sender: str) -> bool:
        """Rank-checked cooperative interrupt by a peer agent. Delegates to
        ``mailbox._try_interrupt`` so rank + per-round quota rules are honoured.
        """
        return self._mailbox._try_interrupt(self.name, sender)

    def cancel(self, reason: str = "⏹ 已停止") -> None:
        """Hard stop: cooperative interrupt + ``task.cancel()`` (the backstop
        for non-checkpointed blocking). The in-flight LLM/streaming call
        unwinds within ``tool_loop._CANCEL_UNWIND_TIMEOUT`` — it cannot block
        the loop indefinitely (the 2026-07-08 Harper 96s hang).
        """
        self.force_interrupt(sender="用户", reason=reason)
        t = self.task
        if t is not None and not t.done():
            t.cancel()
            logger.info("AgentRunner: cancel {} task ({})", self.name, reason)
