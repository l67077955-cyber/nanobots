"""AgentRunner — per-agent runtime handle (busy/idle + cancel signal).

Owns:
- ``_busy`` / ``_waiting`` lifecycle (tool_loop in flight / mailbox wait)
- cooperative cancel signal (``interrupt_event``, shared with MailboxHub)

Does **not** own the asyncio task registry (still on ``engine._broadcast_tasks``)
or the shared conversation History (``engine.history``).

MailboxHub may *read* busy via engine-wired ``get_busy_agents``; it must not
be the write path. Call ``begin_cycle`` / ``end_cycle`` only.

See ``docs/groupchat-coupling-fix.md`` and ``nanobot/groupchat/runtime/ports.py``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Optional

from loguru import logger

if TYPE_CHECKING:
    from nanobot.groupchat.runtime.mailbox import MailboxHub


class AgentRunner:
    """Per-agent runtime handle that OWNS the busy state.

    Single write path for busy/idle: ``begin_cycle`` / ``end_cycle``.
    Mailbox interrupt targeting reads busy through the engine callback over
    ``runner.is_busy`` — it does not maintain a parallel authoritative set.
    """

    def __init__(
        self,
        name: str,
        mailbox: "MailboxHub",
        task_getter: Callable[[], Optional[asyncio.Task]],
    ) -> None:
        self.name = name
        self._mailbox = mailbox
        self._task_getter = task_getter
        # ── Owned state ───────────────────────────────────────────────────
        self._busy: bool = False  # inside tool_loop
        self._waiting: bool = False  # blocked on mailbox.wait

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

    # ── Owned state (no longer derived from mailbox) ─────────────────────

    @property
    def is_busy(self) -> bool:
        """Whether the agent is inside tool_loop (owned state)."""
        return self._busy

    @property
    def is_waiting(self) -> bool:
        """Whether agent is blocked on mailbox.wait (owned state, detail of idle)."""
        return self._waiting

    @property
    def state(self) -> str:
        """busy | idle | done (three-tier model).

        busy = tool_loop racing interrupt
        idle = no tool_loop in flight
        done = task completed (terminal state)
        """
        t = self.task
        if t is None or t.done():
            return "done"
        if self._busy:
            return "busy"
        return "idle"

    @property
    def interrupt_pending(self) -> bool:
        """Whether an interrupt event is set (detail of idle, not a state tier)."""
        return self.interrupt_event.is_set()

    # ── Cycle state machine ──────────────────────────────────────────────
    # Sole write path for busy/idle. Mailbox must not be updated here.

    def begin_cycle(self) -> None:
        """Mark the agent busy (entering tool_loop)."""
        self._busy = True

    def end_cycle(self) -> None:
        """Mark the agent idle (tool_loop exited)."""
        self._busy = False

    def set_waiting(self, waiting: bool) -> None:
        """Update waiting state (called by mailbox.wait)."""
        self._waiting = waiting

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
