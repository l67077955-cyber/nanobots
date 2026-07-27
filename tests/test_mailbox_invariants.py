"""Tests for MailboxHub invariants.

Key invariants:
1. broadcast(to=["All"]) reaches all agents except sender
2. targeted send reaches specific recipients
3. no recipient → message still added to history
4. engine stop → all pending messages cleared
"""

import asyncio
import pytest

from nanobot.groupchat.orchestra.mailbox import MailboxHub


class TestBroadcast:
    """Broadcast messaging invariants."""

    @pytest.mark.asyncio
    async def test_broadcast_reaches_all(self):
        """Message to 'All' should be received by every other agent."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.create("Bob")
        hub.create("Carol")
        hub.start_round(active_agents=["Alice", "Bob", "Carol"])

        hub.send("Alice", ["All"], "Hello everyone!")

        # Bob and Carol should receive the message (not Alice - sender)
        msg_b = await hub.wait("Bob", timeout=1.0)
        msg_c = await hub.wait("Carol", timeout=1.0)

        assert msg_b is not None and msg_b.content == "Hello everyone!"
        assert msg_c is not None and msg_c.content == "Hello everyone!"

    @pytest.mark.asyncio
    async def test_broadcast_history_recorded(self):
        """Broadcast messages should appear in history."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.create("Bob")
        hub.start_round()

        hub.send("Alice", ["All"], "Test message")

        assert len(hub._history) == 1
        assert hub._history[0].content == "Test message"


class TestTargetedSend:
    """Targeted messaging invariants."""

    @pytest.mark.asyncio
    async def test_targeted_reaches_specific(self):
        """Message to specific agent should only reach that agent."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.create("Bob")
        hub.create("Carol")
        hub.start_round()

        hub.send("Alice", ["Bob"], "Secret for Bob")

        # Bob should receive it
        msg_b = await hub.wait("Bob", timeout=1.0)
        assert msg_b is not None
        assert msg_b.content == "Secret for Bob"

        # Carol should not receive it (queue should be empty)
        # Use get_nowait to check without blocking
        carol_queue = hub._queues.get("Carol")
        assert carol_queue is not None
        assert carol_queue.empty()

    @pytest.mark.asyncio
    async def test_multiple_targets(self):
        """Message to multiple specific agents should reach all of them."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.create("Bob")
        hub.create("Carol")
        hub.start_round()

        hub.send("Alice", ["Bob", "Carol"], "Hi Bob and Carol")

        msg_b = await hub.wait("Bob", timeout=1.0)
        msg_c = await hub.wait("Carol", timeout=1.0)

        assert msg_b.content == "Hi Bob and Carol"
        assert msg_c.content == "Hi Bob and Carol"

        # Alice should not receive her own message
        alice_queue = hub._queues.get("Alice")
        assert alice_queue is not None
        assert alice_queue.empty()


class TestNoRecipient:
    """Edge case: no valid recipient."""

    @pytest.mark.asyncio
    async def test_send_to_nonexistent_agent(self):
        """Sending to non-existent agent should still be recorded."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.start_round()

        # Should not raise, message goes to history
        hub.send("Alice", ["Ghost"], "Are you there?")

        assert len(hub._history) == 1

    @pytest.mark.asyncio
    async def test_send_empty_targets(self):
        """Sending with empty target list should still be recorded."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.start_round()

        hub.send("Alice", [], "Message to nowhere")

        assert len(hub._history) == 1


class TestEngineStop:
    """Engine stop clears all pending state."""

    def test_start_round_clears_queues(self):
        """start_round should clear all message queues."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.create("Bob")
        hub.start_round()

        # Send some messages
        hub.send("Alice", ["Bob"], "Message 1")
        hub.send("Bob", ["Alice"], "Message 2")

        # Start new round
        hub.start_round()

        # Queues should be empty
        assert hub._queues["Alice"].empty()
        assert hub._queues["Bob"].empty()

    def test_start_round_clears_history(self):
        """start_round should clear message history."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.start_round()

        hub.send("Alice", ["Bob"], "Test")

        hub.start_round()

        assert len(hub._history) == 0

    def test_start_round_clears_interrupt_state(self):
        """start_round should reset interrupt counts and busy state."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.start_round()

        # Simulate some interrupt state
        hub._interrupt_counts["Alice"] = 2
        hub._busy_agents.add("Alice")

        hub.start_round()

        assert len(hub._interrupt_counts) == 0
        assert len(hub._busy_agents) == 0


class TestAgentLifecycle:
    """Agent creation and removal invariants."""

    def test_create_idempotent(self):
        """Creating same agent twice should not raise."""
        hub = MailboxHub()
        hub.create("Alice")
        hub.create("Alice")  # Should not raise

        assert "Alice" in hub._queues

    def test_create_produces_empty_queue(self):
        """New agent mailbox should start empty."""
        hub = MailboxHub()
        hub.create("Alice")

        assert hub._queues["Alice"].empty()
