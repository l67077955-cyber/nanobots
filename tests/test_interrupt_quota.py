"""Guard: interrupt quota is NOT refunded per handled interrupt.

Regression for the 索求↔交付 dead-loop: broadcast used to decrement
_interrupt_counts after every handled interrupt, which refunded the per-round
quota (>=3 in _try_interrupt) and let a leader↔agent status/delivery ping-pong
interrupt each other forever. mailbox reset_round() clears the quota each round,
so dropping the broadcast-side refund keeps the guard effective without papering
over user interrupts (which go via interrupt_busy_agents and bypass this quota).
"""
import asyncio

import pytest

from nanobot.groupchat.orchestra.mailbox import MailboxHub


def _hub(names):
    mb = MailboxHub()
    mb.start_round(names)
    # Give B a higher rank so B may interrupt A (equal rank cannot interrupt).
    mb.set_ranks(ranks={"A": "knight", "B": "bishop"}, leader=None)
    return mb


def test_try_interrupt_quota_caps_per_round():
    mb = _hub(["A", "B"])
    mb.start_round(["A", "B"])  # fresh round: counters cleared
    mb.mark_busy("A")

    # 3 successful interrupts fill the quota (count -> 3)
    n = 0
    for i in range(5):
        if mb._try_interrupt("A", "B"):
            n += 1
    assert n == 3, f"expected exactly 3 interrupts (quota), got {n}"
    assert mb._interrupt_counts["A"] == 3

    # Quota exhausted: further agent->agent interrupts are rejected
    assert not mb._try_interrupt("A", "B")


def test_user_interrupt_busy_agents_bypasses_quota():
    mb = _hub(["A", "B"])
    mb.start_round(["A", "B"])
    mb.mark_busy("A")

    # Fill the agent-agent quota
    for _ in range(3):
        mb._try_interrupt("A", "B")
    assert mb._interrupt_counts["A"] == 3
    assert not mb._try_interrupt("A", "B")

    # Simulate the handled-interrupt cycle: broadcast clears the event after
    # the agent responds. A fresh user interrupt (the real case: agent went
    # idle, evt cleared) must fire and RESET the count so later user messages
    # keep working — the agent-agent quota must not leak over.
    mb.get_interrupt_event("A").clear()
    fired = mb.interrupt_busy_agents("用户")
    assert fired >= 1
    assert mb._interrupt_counts["A"] == 0, "user interrupt resets agent quota"


@pytest.mark.asyncio
async def test_reset_round_refreshes_quota():
    mb = _hub(["A", "B"])
    mb.start_round(["A", "B"])
    mb.mark_busy("A")
    for _ in range(3):
        mb._try_interrupt("A", "B")
    assert not mb._try_interrupt("A", "B")

    # New round clears counters -> quota restored
    mb.start_round(["A", "B"])
    assert mb._interrupt_counts == {}
    mb.mark_busy("A")
    assert mb._try_interrupt("A", "B")