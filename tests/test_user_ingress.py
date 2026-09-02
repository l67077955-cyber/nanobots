"""Behavioral tests for UserIngress — the single decision point for
messages consumed from engine._input_queue.

Includes the regression test for the phantom-method crash: the legacy
``_user_listener`` called ``mailbox.is_discussion_ended()`` which does not
exist, so a user message arriving during a LIVE round raised AttributeError,
killed the listener, and silently dropped the message. The behavioral
equivalent is pinned here — delivery during an active round must succeed.
"""

from __future__ import annotations

import asyncio

from nanobot.groupchat.orchestra.mailbox import ConversationPool, MailboxHub
from nanobot.groupchat.orchestra.round_lifecycle import RoundLifecycle
from nanobot.groupchat.orchestra.user_ingress import (
    SUMMARY_SENTINEL,
    IngressAction,
    UserIngress,
)


class _FakeEngine:
    def __init__(self, agents):
        self._input_queue: asyncio.Queue = asyncio.Queue()
        self._active_agents = list(agents)
        self._running = True
        self._summary_requested = False
        self.history: list[tuple[str, str]] = []
        self.sent: list[str] = []

    def _add_message(self, sender: str, content: str) -> None:
        self.history.append((sender, content))

    async def _send(self, text: str) -> None:
        self.sent.append(text)


def _hub(names):
    mb = MailboxHub()
    for n in names:
        mb.create(n)
    mb.start_round(names)
    return mb


def _ingress(agents=("Kirk", "Harper"), *, with_pool=True):
    engine = _FakeEngine(agents)
    mb = _hub(list(agents))
    lc = RoundLifecycle(engine=engine)
    pool = ConversationPool(capacity=3, agents=list(agents)) if with_pool else None
    return UserIngress(engine, mb, lc, pool=pool), engine, mb, lc, pool


class TestMidRoundInterjection:
    async def test_active_round_delivery(self):
        """REGRESSION: the phantom-method crash scenario — message during a
        live round must be delivered, not raise."""
        ingress, engine, mb, _lc, _pool = _ingress()
        action = await ingress.handle_round_message("你们聊到哪了")
        assert action is IngressAction.DELIVERED
        # Delivered into every agent mailbox
        for name in ("Kirk", "Harper"):
            msg = mb._queues[name].get_nowait()
            assert msg.sender == "用户"
            assert msg.content == "你们聊到哪了"
        # Recorded into shared history
        assert ("用户", "你们聊到哪了") in engine.history
        # Uniform echo emitted
        assert engine.sent and engine.sent[0].startswith("── User ──")

    async def test_delivery_interrupts_busy_agents(self):
        ingress, _engine, mb, _lc, _pool = _ingress()
        mb.mark_busy("Kirk")
        await ingress.handle_round_message("醒醒")
        evt = mb.get_interrupt_event("Kirk")
        assert evt.is_set()

    async def test_delivery_allocates_pool_slot_per_recipient(self):
        ingress, _engine, _mb, _lc, pool = _ingress()
        before = pool.agent_available("Kirk")
        await ingress.handle_round_message("占一个名额")
        assert pool.agent_available("Kirk") == before - 1

    async def test_no_pool_still_delivers(self):
        ingress, engine, mb, _lc, pool = _ingress(with_pool=False)
        assert pool is None
        action = await ingress.handle_round_message("没有pool也行")
        assert action is IngressAction.DELIVERED
        assert not mb._queues["Kirk"].empty()


class TestWindingDownRequeue:
    async def test_requeues_and_parks(self):
        ingress, engine, _mb, lc, _pool = _ingress()
        lc.mark_winding_down("leader_end_discussion", flip_running=True)
        action = await ingress.handle_round_message("稍后的消息")
        assert action is IngressAction.REQUEUED
        # Message is back in the queue for the next round…
        assert engine._input_queue.get_nowait() == "稍后的消息"
        # …a notice was shown…
        assert any("已排队" in s for s in engine.sent)
        # …and the consumer must stop polling (parked).
        assert ingress.parked is True

    async def test_requeue_not_recorded_into_history(self):
        ingress, engine, _mb, lc, _pool = _ingress()
        lc.mark_winding_down("global_timeout")
        await ingress.handle_round_message("排队消息")
        assert ("用户", "排队消息") not in engine.history


class TestSummarySentinel:
    async def test_mid_round_summary_defers_without_noise(self):
        ingress, engine, _mb, _lc, _pool = _ingress()
        action = await ingress.handle_round_message(SUMMARY_SENTINEL)
        assert action is IngressAction.SUMMARY_DEFERRED
        assert engine._summary_requested is True
        assert engine.sent == []  # no receipt for the sentinel
        assert not ingress.parked

    async def test_winding_down_summary_still_defers(self):
        ingress, engine, _mb, lc, _pool = _ingress()
        lc.mark_winding_down("converged")
        action = await ingress.handle_round_message(SUMMARY_SENTINEL)
        assert action is IngressAction.SUMMARY_DEFERRED
        assert ingress.parked is False


class TestRoundOpen:
    async def test_receipt_counts_all_agents(self):
        ingress, engine, _mb, _lc, _pool = _ingress(agents=("Kirk", "Harper", "Benjamin"))
        action = await ingress.open_round("继续")
        assert action is IngressAction.ROUND_OPENED
        assert ("用户", "继续") in engine.history
        assert any("3 个 agent" in s for s in engine.sent)

    async def test_single_agent_also_gets_receipt(self):
        """The legacy 📮 receipt was gated on >= 2 agents; single-agent
        groups silently got nothing. Uniform policy: everyone gets one."""
        ingress, engine, _mb, _lc, _pool = _ingress(agents=("Solo",))
        await ingress.open_round("单独聊")
        assert any("1 个 agent" in s for s in engine.sent)
