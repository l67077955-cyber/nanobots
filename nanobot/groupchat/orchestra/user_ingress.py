"""UserIngress — the single decision point for messages from engine._input_queue.

Historically a user message had three unrelated code paths (mid-round
interjection in ``_user_listener``, requeue-at-round-boundary, new-round
open in ``run_loop``), each deciding on its own whether to show any
acknowledgement — which is why messages late in a session could vanish
without a trace. This module centralises that decision so every path
shares one delivery sequence and one acknowledgement policy:

- ``handle_round_message`` — during a live round (round-scoped listener):
  user text is interjected into the mailbox + history with a uniform
  ``── User ─`` echo; ``__SUMMARY__`` defers to run_loop; anything arriving
  after the round started winding down is requeued with an "queued" notice
  and the consumer parks (never races run_loop on the queue).
- ``open_round`` — between rounds (run_loop): records the message and
  emits the round-opening receipt (works for single-agent groups too).

All acknowledgement strings for user messages originate here.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from loguru import logger

from nanobot.groupchat.display.display import user_interjection_msg
from nanobot.groupchat.orchestra.events import get_bus
from nanobot.groupchat.orchestra.round_lifecycle import RoundLifecycle

SUMMARY_SENTINEL = "__SUMMARY__"

ROUND_OPEN_RECEIPT = "📮 已收到用户消息，{n} 个 agent 开始处理"
QUEUED_NOTICE = "📥 本轮正在收尾，消息已排队，本轮结束后立即处理"


class IngressAction(Enum):
    DELIVERED = "delivered"            # interjected into the live round
    REQUEUED = "requeued"              # round winding down; parked for next round
    SUMMARY_DEFERRED = "summary_deferred"  # __SUMMARY__ sentinel; handled after round
    ROUND_OPENED = "round_opened"      # between rounds; new round starting


async def open_round_with(engine: Any, user_input: str) -> IngressAction:
    """Between-rounds entry used by run_loop: record + uniform receipt.

    Kept module-level because run_loop consumes messages outside any live
    round (no mailbox/lifecycle in scope there yet); open_round only ever
    touches the engine.
    """
    if user_input == SUMMARY_SENTINEL:  # defensive; run_loop handles first
        engine._summary_requested = True
        return IngressAction.SUMMARY_DEFERRED
    engine._add_message("用户", user_input)
    n = len(engine._active_agents)
    if n:
        await engine._send(ROUND_OPEN_RECEIPT.format(n=n))
    await get_bus().emit("user:round_opened", engine=engine, user_input=user_input, agent_count=n)
    return IngressAction.ROUND_OPENED


class UserIngress:
    """Classify and dispatch everything consumed from ``engine._input_queue``.

    Parameters
    ----------
    engine:
        GroupChatEngine-like object; needs ``_input_queue``, ``_active_agents``,
        ``_add_message``, ``_send`` (and ``_summary_requested`` for the sentinel).
    mailbox:
        Live ``MailboxHub`` for delivery into agent queues.
    lifecycle:
        The round's ``RoundLifecycle``; interjection is only attempted while
        it ``accepts_interjection()``.
    pool:
        Optional ``ConversationPool``; when present, user delivery force-allocates
        one slot per recipient and the pool status line is appended to the echo.
    """

    def __init__(
        self,
        engine: Any,
        mailbox: Any,
        lifecycle: RoundLifecycle,
        *,
        pool: Any = None,
    ) -> None:
        self._engine = engine
        self._mailbox = mailbox
        self._lifecycle = lifecycle
        self._pool = pool
        # Set once a message has been requeued because the round was ending.
        # The round-scoped consumer must stop polling the queue from then on:
        # run_loop drains it for the next round, and a second consumer here
        # would both race run_loop and deliver into mailboxes nobody reads.
        self.parked = False

    # ── During a live round ────────────────────────────────────────────────

    async def handle_round_message(self, msg: str) -> IngressAction:
        """Dispatch one message consumed while a round is in flight."""
        if msg == SUMMARY_SENTINEL:
            self._engine._summary_requested = True
            await get_bus().emit("summary:deferred", engine=self._engine)
            return IngressAction.SUMMARY_DEFERRED

        if not self._lifecycle.accepts_interjection():
            self._engine._input_queue.put_nowait(msg)
            self.parked = True
            logger.info(
                "UserIngress: round winding down — message requeued for next round: {}",
                msg[:60],
            )
            await get_bus().emit("user:message_requeued", engine=self._engine, message=msg)
            await self._engine._send(QUEUED_NOTICE)
            return IngressAction.REQUEUED

        return await self._interject(msg)

    # ── Between rounds ─────────────────────────────────────────────────────

    async def open_round(self, user_input: str) -> IngressAction:
        """Record a user message that opens a new round and emit its receipt.

        Delegates to the module-level ``open_round_with`` (also used directly
        by run_loop between rounds).
        """
        return await open_round_with(self._engine, user_input)

    # ── Delivery ───────────────────────────────────────────────────────────

    async def _interject(self, msg: str) -> IngressAction:
        """Deliver a mid-round user message: pool slot, mailbox, interrupt, history, echo."""
        all_agent_names = list(self._mailbox.agent_names)
        if self._pool is not None:
            await self._pool.allocate_user(all_agent_names)

        self._mailbox.create("用户")
        delivered = self._mailbox.send("用户", ["All"], msg)
        interrupted = self._mailbox.interrupt_busy_agents("用户")
        self._engine._add_message("用户", msg)

        echo = user_interjection_msg(msg)
        if self._pool is not None:
            echo = f"{echo}\n  {self._pool.status()}"
        await self._engine._send(echo)
        logger.info(
            "UserIngress: interjected ({} delivered, {} interrupted): {}",
            delivered, interrupted, msg[:60],
        )
        await get_bus().emit(
            "user:message_delivered",
            engine=self._engine, message=msg,
            delivered_to=delivered, interrupted=interrupted,
        )
        return IngressAction.DELIVERED
