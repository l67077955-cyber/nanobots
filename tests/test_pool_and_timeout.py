"""Behavioral tests for ConversationPool (pool accounting) and MailboxHub (timeout).

These exercise the real runtime semantics — slot acquire/release, user-message
accounting, and wait() timeout/deadline behavior — not source-text assertions.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.orchestra.mailbox import ConversationPool, MailboxHub


# ------------------------------------------------------------------
# ConversationPool
# ------------------------------------------------------------------

def _pool(agents=None, capacity=3):
    return ConversationPool(capacity=capacity, agents=agents or ["Alpha", "Beta", "Gamma"])


class TestPoolBasicAccounting:
    def test_initial_available_equals_capacity(self):
        p = _pool()
        assert p.available == 3 * 3
        for a in ["Alpha", "Beta", "Gamma"]:
            assert p.agent_available(a) == 3

    def test_per_agent_capacity_wins(self):
        p = ConversationPool(capacity=0, agents=["A", "B"],
                             per_agent_capacity={"A": 2, "B": 5})
        assert p.agent_capacity("A") == 2
        assert p.agent_capacity("B") == 5

    def test_unknown_agent_available_is_zero(self):
        p = _pool()
        assert p.agent_available("Ghost") == 0


class TestPoolAllocate:
    @pytest.mark.asyncio
    async def test_allocate_consumes_sender_slots(self):
        p = _pool()
        ok = await p.allocate("Alpha", ["Beta", "Gamma"])
        assert ok is True
        # Alpha paid 2 slots (one per recipient).
        assert p.agent_available("Alpha") == 1
        # Recipients' own pools are untouched by a normal send.
        assert p.agent_available("Beta") == 3
        assert p.agent_available("Gamma") == 3

    @pytest.mark.asyncio
    async def test_allocate_tracks_pending_on_recipient(self):
        p = _pool()
        await p.allocate("Alpha", ["Beta"])
        # Beta now "expects" to reply to Alpha.
        assert "Alpha" in p._pending["Beta"]

    @pytest.mark.asyncio
    async def test_allocate_exceeding_capacity_charges_cap_only(self):
        # Broadcast affordability: a wide send costs at most the sender's full
        # pool (min(n, cap)) instead of being structurally rejected — low-cap
        # agents (pawn=2 in a 3-agent room) must still address the whole room.
        p = _pool(capacity=2)
        ok = await p.allocate("Alpha", ["Beta", "Gamma", "Alpha"])  # n=3 > cap=2
        assert ok is True
        assert p.agent_available("Alpha") == 0  # charged exactly cap=2
        # Pending markers stay 1:1 with slots — only the charged recipients
        # hold an "expects reply" marker, so release_unread never over-refunds.
        assert "Alpha" in p._pending["Beta"]
        assert "Alpha" in p._pending["Gamma"]
        assert p._pending.get("Alpha") == [] or "Alpha" not in p._pending["Alpha"]

    @pytest.mark.asyncio
    async def test_allocate_zero_capacity_rejected(self):
        p = ConversationPool(capacity=0, agents=["A", "B"],
                             per_agent_capacity={"A": 0, "B": 2})
        ok = await p.allocate("A", ["B"])
        assert ok is False
        assert p.agent_available("A") == 0


class TestPoolRelease:
    @pytest.mark.asyncio
    async def test_release_unread_returns_slots_to_sender(self):
        p = _pool()
        await p.allocate("Alpha", ["Beta"])
        # Beta waits without replying -> slots freed back to Alpha.
        released = p.release_unread("Beta")
        assert released == 1
        assert p.agent_available("Alpha") == 3

    @pytest.mark.asyncio
    async def test_mark_replied_returns_slots_to_sender(self):
        p = _pool()
        await p.allocate("Alpha", ["Beta"])
        p.mark_replied("Beta", "Alpha")  # Beta replied to Alpha
        assert p.agent_available("Alpha") == 3

    @pytest.mark.asyncio
    async def test_empty_pending_release_is_noop(self):
        p = _pool()
        assert p.release_unread("Alpha") == 0


class TestPoolUserAllocation:
    """User messages force-allocate slots from each recipient's OWN pool.

    Key invariant: a slot deducted for a user message must be released back to
    that agent's own pool when the agent replies (or when it waits).  This was
    BROKEN — the "User" pseudo-sender has no semaphore, so `mark_replied` /
    `release_unread` could never return the slot, causing pool exhaustion over
    time (and a hard deadlock once every agent is pool-full).
    """

    @pytest.mark.asyncio
    async def test_user_message_consumes_own_slot(self):
        p = _pool()
        await p.allocate_user(["Alpha", "Beta"])
        assert p.agent_available("Alpha") == 2  # 3 - 1
        assert p.agent_available("Beta") == 2
        # Gamma (no message) untouched
        assert p.agent_available("Gamma") == 3

    @pytest.mark.asyncio
    async def test_user_slot_restored_when_agent_waits(self):
        p = _pool()
        await p.allocate_user(["Alpha"])
        assert p.agent_available("Alpha") == 2
        # Agent goes to wait -> user slot must return to its own pool.
        p.release_unread("Alpha")
        assert p.agent_available("Alpha") == 3

    @pytest.mark.asyncio
    async def test_user_slot_restored_when_agent_replies_to_user(self):
        p = _pool()
        await p.allocate_user(["Alpha"])
        assert p.agent_available("Alpha") == 2
        # Agent replies to the user broadcast (sender tag "用户").
        p.mark_replied("Alpha", "用户")
        assert p.agent_available("Alpha") == 3

    @pytest.mark.asyncio
    async def test_repeated_user_messages_do_not_exhaust_pool(self):
        p = _pool(capacity=3, agents=["Alpha"])
        for _ in range(10):
            await p.allocate_user(["Alpha"])
            p.release_unread("Alpha")  # agent processes + waits after each
        # After each cycle the slot returns; pool must never be exhausted.
        assert p.agent_available("Alpha") > 0
        assert p.agent_available("Alpha") == 3


# ------------------------------------------------------------------
# MailboxHub wait() timeout behavior
# ------------------------------------------------------------------

class TestMailboxWaitTimeout:
    @pytest.mark.asyncio
    async def test_wait_returns_none_on_timeout(self):
        hub = MailboxHub()
        hub.create("Alpha")
        # Nothing ever arrives -> must return None after the short timeout.
        msg = await hub.wait("Alpha", timeout=0.05)
        assert msg is None

    @pytest.mark.asyncio
    async def test_wait_returns_message_when_available(self):
        hub = MailboxHub()
        hub.create("Alpha")
        hub.create("Beta")
        hub.send("Beta", ["Alpha"], "hello")
        msg = await hub.wait("Alpha", timeout=5)
        assert msg is not None
        assert msg.content == "hello"
        assert msg.sender == "Beta"

    @pytest.mark.asyncio
    async def test_fast_path_returns_queued_message(self):
        hub = MailboxHub()
        hub.create("Alpha")
        hub.create("Beta")
        hub.send("Beta", ["Alpha"], "hello")
        # First wait drains the queue immediately (fast path).
        msg = await hub.wait("Alpha", timeout=5)
        assert msg.content == "hello"
        # Second wait has nothing -> times out quickly.
        msg2 = await hub.wait("Alpha", timeout=0.05)
        assert msg2 is None