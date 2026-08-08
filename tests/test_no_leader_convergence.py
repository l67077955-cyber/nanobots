"""Test the no-leader convergence foundation: mailbox all-waiting detection.

The no-leader convergence sentinel (broadcast.py) ends a leaderless round when
every agent is simultaneously waiting and the history is stable. It keys off
mailbox.all_waiting_event. These tests verify that event fires correctly.
"""
from __future__ import annotations

import asyncio
import pytest

from nanobot.groupchat.orchestra.mailbox import MailboxHub


def _hub(names):
    mb = MailboxHub(names)
    for n in names:
        mb.create(n)
    mb.start_round(active_agents=list(names))
    return mb


@pytest.mark.asyncio
async def test_all_waiting_event_fires_when_all_agents_wait():
    mb = _hub(["Alpha", "Beta"])

    t1 = asyncio.create_task(mb.wait("Alpha", timeout=0.2))
    t2 = asyncio.create_task(mb.wait("Beta", timeout=0.2))
    # Small sleep so both enter their wait loop
    await asyncio.sleep(0.1)

    assert mb.all_waiting_event.is_set(), (
        "all_waiting_event should fire when every agent is waiting"
    )
    await asyncio.gather(t1, t2, return_exceptions=True)


@pytest.mark.asyncio
async def test_all_waiting_event_partial_wait_does_not_fire():
    mb = _hub(["A", "B", "C"])

    t1 = asyncio.create_task(mb.wait("A", timeout=1.0))
    await asyncio.sleep(0.1)
    assert not mb.all_waiting_event.is_set(), "only A waiting -> not all waiting"

    t2 = asyncio.create_task(mb.wait("B", timeout=1.0))
    await asyncio.sleep(0.1)
    assert not mb.all_waiting_event.is_set(), "A+B waiting, C active -> no fire"

    t3 = asyncio.create_task(mb.wait("C", timeout=1.0))
    await asyncio.sleep(0.2)
    assert mb.all_waiting_event.is_set(), "all three waiting -> fires"
    await asyncio.gather(t1, t2, t3, return_exceptions=True)


@pytest.mark.asyncio
async def test_all_waiting_cleared_on_start_round():
    mb = _hub(["A", "B"])
    t1 = asyncio.create_task(mb.wait("A", timeout=0.2))
    t2 = asyncio.create_task(mb.wait("B", timeout=0.2))
    await asyncio.sleep(0.1)
    assert mb.all_waiting_event.is_set()
    await asyncio.gather(t1, t2, return_exceptions=True)

    # New round resets it
    mb.start_round(active_agents=["A", "B"])
    assert not mb.all_waiting_event.is_set()