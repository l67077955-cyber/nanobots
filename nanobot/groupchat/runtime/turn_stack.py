"""TurnStack — turn-level operations seam for a broadcast round.

Important design note: the engine runs agents **concurrently** as asyncio tasks
(launched together at round start), not as a sequential queue. So this is NOT
a FIFO of pending turns — it is the single seam for the turn-level operations
that cut *across* all agents mid-round:

- ``interject(user_msg)``  — inject a user message into the live round
- ``cancel_all()``          — cancel every in-flight agent task (the /stop path)
- ``active_agents``         — who is running this round

Before this seam, ``interject`` lived inline in ``broadcast._user_listener``
and ``cancel_all`` inline in ``engine._stop_group_loop`` — two more places
reaching directly into mailbox/pool/engine internals. Routing them here plants
the third port (after ``AgentRunner`` and ``History``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanobot.groupchat.runtime.engine import GroupChatEngine
    from nanobot.groupchat.runtime.mailbox import ConversationPool, MailboxHub


class TurnStack:
    """Concrete ``ports.TurnStack`` — turn-level ops facade (delegating)."""

    def __init__(
        self,
        engine: "GroupChatEngine",
        mailbox: "MailboxHub",
        pool: "ConversationPool | None",
        agent_names: list[str],
    ) -> None:
        self._engine = engine
        self._mailbox = mailbox
        self._pool = pool
        self._agent_names = list(agent_names)

    @property
    def active_agents(self) -> list[str]:
        return list(self._agent_names)

    def _round_winding_down(self) -> bool:
        """Round is ending (engine stopped / discussion ended / all tasks done)."""
        if not self._engine._running:
            return True
        if self._mailbox.is_discussion_ended():
            return True
        tasks = list(self._engine._broadcast_tasks.values())
        if tasks and all(t.done() for t in tasks):
            return True
        return False

    async def interject(self, user_msg: str) -> bool:
        """Inject a user message into the live round.

        Force-allocates a pool slot from each recipient, broadcasts to all
        agents, interrupts busy agents so they pick up the message at the next
        safe checkpoint, records + displays the message.

        Returns True if injected. Returns False (after requeuing the message
        onto ``engine._input_queue``) if the round is winding down — so the
        caller (the user listener) exits and ``run_loop`` processes the message
        as a fresh round instead of silently swallowing it.
        """
        if self._round_winding_down():
            self._engine._input_queue.put_nowait(user_msg)
            logger.info(
                "TurnStack: round ending — user message requeued for next round: {}",
                user_msg[:60],
            )
            return False

        all_agent_names = list(self._mailbox.agent_names)
        if self._pool is not None:
            await self._pool.allocate_user(all_agent_names)

        self._mailbox.create("用户")
        self._mailbox.send("用户", ["All"], user_msg)
        # Interrupt agents currently inside tool_loop so they pick up the user
        # message at the next safe checkpoint rather than waiting for their
        # current tool batch to finish.
        interrupted = self._mailbox.interrupt_busy_agents("用户")
        self._engine._add_message("用户", user_msg)
        await self._engine._send(
            f"── User ──\n{user_msg}\n"
            f"  {self._pool.status() if self._pool else ''}"
        )
        logger.info(
            "TurnStack: user interjected: {} ({} agent(s) interrupted)",
            user_msg[:60], interrupted,
        )
        return True

    def cancel_all(self) -> int:
        """Cancel every in-flight broadcast agent task for this round.

        Routed here from ``engine._stop_group_loop`` so the /stop path goes
        through the seam. Returns the number of tasks cancelled.
        """
        count = 0
        for name, task in list(self._engine._broadcast_tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
                logger.info("TurnStack: cancelled broadcast task for {}", name)
        return count
