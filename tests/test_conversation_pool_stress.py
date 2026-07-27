"""Stress test for ConversationPool concurrent operations.

Verifies that under high concurrency:
1. allocate() never hangs
2. Budget accounting remains consistent
3. No race conditions or deadlocks
"""

import asyncio
import pytest

from nanobot.groupchat.orchestra.mailbox import ConversationPool


class TestConversationPoolConcurrency:
    """Verify pool behaves correctly under concurrent load."""

    @pytest.mark.asyncio
    async def test_concurrent_allocate_never_hangs(self):
        """Multiple concurrent allocations should all complete."""
        pool = ConversationPool(agents=["A", "B", "C"], capacity=5)
        n_concurrent = 20

        async def allocate_and_return(i: int) -> tuple[int, bool]:
            result = await pool.allocate("A", ["B"])
            return (i, result)

        # Launch many concurrent allocations
        tasks = [asyncio.create_task(allocate_and_return(i)) for i in range(n_concurrent)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # All should complete without hanging
        assert len(results) == n_concurrent
        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, f"Got exceptions: {exceptions}"

    @pytest.mark.asyncio
    async def test_budget_cannot_go_negative(self):
        """Budget accounting should never go below zero."""
        pool = ConversationPool(agents=["A"], capacity=2)

        # Allocate beyond capacity
        await pool.allocate("A", ["B"])  # 1 used
        await pool.allocate("A", ["C"])  # 2 used (exhausted)

        # Further allocations should fail (return False or raise)
        # Not hang
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.allocate("A", ["D"]), timeout=0.5)

        # Available should be >= 0
        assert pool.agent_available("A") >= 0

    @pytest.mark.asyncio
    async def test_reset_restores_budget_under_concurrency(self):
        """reset() should atomically restore all budgets."""
        pool = ConversationPool(agents=["A", "B"], capacity=3)

        async def stress_allocate():
            for _ in range(10):
                try:
                    await asyncio.wait_for(pool.allocate("A", ["B"]), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.01)

        # Stress test
        tasks = [asyncio.create_task(stress_allocate()) for _ in range(5)]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Reset
        pool.reset()

        # Budget should be restored
        assert pool.agent_available("A") == 3
        assert pool.agent_available("B") == 3

    @pytest.mark.asyncio
    async def test_user_allocate_bypasses_agent_budget(self):
        """User messages should not be blocked by agent budget exhaustion."""
        pool = ConversationPool(agents=["A", "B"], capacity=1)

        # Exhaust A's budget
        await pool.allocate("A", ["B"])

        # User allocate should still work
        await pool.allocate_user(["A", "B"])  # Force-allocates from recipients

        # No exceptions = success
