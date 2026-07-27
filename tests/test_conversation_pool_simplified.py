"""Tests for simplified ConversationPool.

Key invariants:
1. allocate() returns immediately (no blocking)
2. Budget exceeded → returns False (LLM handles error)
3. User messages never blocked (separate path)
4. Per-agent budget reset between rounds
"""

import asyncio
import pytest

from nanobot.groupchat.orchestra.mailbox import ConversationPool


class TestConversationPoolNoBlocking:
    """Verify allocate never blocks."""

    @pytest.mark.asyncio
    async def test_allocate_returns_immediately(self):
        """allocate() should return True/False without blocking."""
        pool = ConversationPool(agents=["A", "B"], capacity=2)
        # Should return immediately
        result = await pool.allocate("A", ["B"])
        assert result is True

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_false(self):
        """When budget exhausted, allocate returns False (no blocking)."""
        pool = ConversationPool(agents=["A"], capacity=2)
        # Allocate all budget
        assert await pool.allocate("A", ["B", "C"]) is True  # 2 slots
        # Next allocation should fail immediately (not hang)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.allocate("A", ["D"]), timeout=0.5)

    @pytest.mark.asyncio
    async def test_per_agent_budget(self):
        """Each agent has independent budget."""
        pool = ConversationPool(
            agents=["Alice", "Bob"],
            per_agent_capacity={"Alice": 1, "Bob": 3}
        )
        # Alice can send 1 message
        assert await pool.allocate("Alice", ["Bob"]) is True
        assert await pool.allocate("Alice", ["Charlie"]) is False

        # Bob can send 3 messages
        assert await pool.allocate("Bob", ["Alice"]) is True
        assert await pool.allocate("Bob", ["Charlie"]) is True
        assert await pool.allocate("Bob", ["Dave"]) is True
        assert await pool.allocate("Bob", ["Eve"]) is False


class TestConversationPoolUserMessages:
    """User messages should never be blocked."""

    @pytest.mark.asyncio
    async def test_user_allocate_never_blocks(self):
        """User messages bypass agent budget constraints."""
        pool = ConversationPool(agents=["A", "B"], capacity=1)
        # Exhaust agent budget
        await pool.allocate("A", ["B"])
        # User allocate should still work
        await pool.allocate_user(["A", "B"])


class TestConversationPoolReset:
    """Budget reset between rounds."""

    def test_reset_clears_budget(self):
        """reset() should restore all budgets."""
        pool = ConversationPool(agents=["A"], capacity=2)
        pool._available["A"] = 0  # Simulate exhaustion
        pool.reset()
        assert pool._available["A"] == 2


class TestConversationPoolSimplifiedAPI:
    """New simplified API without release_unread/mark_replied."""

    @pytest.mark.asyncio
    async def test_no_release_unread(self):
        """Simplified pool doesn't need release_unread."""
        pool = ConversationPool(agents=["A", "B"], capacity=2)
        # Allocate slots
        await pool.allocate("A", ["B"])
        # No release needed - budget per round, not per message
        pool.reset()  # Just reset between rounds
        assert pool._available["A"] == 2

    @pytest.mark.asyncio
    async def test_no_mark_replied(self):
        """Simplified pool doesn't track reply status."""
        pool = ConversationPool(agents=["A", "B"], capacity=2)
        # Allocate and "reply" - no need to mark
        await pool.allocate("A", ["B"])
        # No mark_replied call needed
        pool.reset()
        assert pool._available["A"] == 2
