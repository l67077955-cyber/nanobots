"""Behavioral tests for the orchestration event bus (events.py).

Pins the load-bearing rules of the mod architecture: zero-listener cost,
per-listener isolation, sync/async emission paths, and the default-bus
singleton used by emitters that were never wired with an instance.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.orchestra.events import (
    BroadcastEventDispatcher,
    get_bus,
    set_bus,
)


@pytest.fixture(autouse=True)
def _isolated_bus():
    set_bus(BroadcastEventDispatcher())
    yield
    set_bus(BroadcastEventDispatcher())


class TestSubscribeEmit:
    async def test_listener_receives_payload(self):
        bus = BroadcastEventDispatcher()
        seen: list[dict] = []
        bus.on("user:message_delivered", lambda **kw: seen.append(kw) or _noop())
        await bus.emit("user:message_delivered", message="hi", delivered_to=2)
        assert seen == [{"message": "hi", "delivered_to": 2}]

    async def test_no_listeners_is_noop(self):
        bus = BroadcastEventDispatcher()
        await bus.emit("round:ended", engine=None)  # must not raise

    def test_off_unsubscribes(self):
        bus = BroadcastEventDispatcher()
        hits: list[int] = []

        async def cb(**kw):
            hits.append(1)

        bus.on("x", cb)
        bus.off("x", cb)
        assert bus.listener_count("x") == 0
        bus.off("x", cb)  # unknown → ignored


class TestIsolation:
    async def test_failing_listener_does_not_break_others(self):
        bus = BroadcastEventDispatcher()
        hits: list[int] = []

        async def bad(**kw):
            raise RuntimeError("boom")

        async def good(**kw):
            hits.append(1)

        bus.on("x", bad)
        bus.on("x", good)
        await bus.emit("x")
        assert hits == [1]

    async def test_emit_nowait_isolates_failures(self):
        bus = BroadcastEventDispatcher()
        hits: list[int] = []

        async def bad(**kw):
            raise RuntimeError("boom")

        async def good(**kw):
            hits.append(1)

        bus.on("x", bad)
        bus.on("x", good)
        bus.emit_nowait("x")
        await asyncio.sleep(0.05)
        assert hits == [1]


class TestEmitNowait:
    async def test_schedules_listeners_when_loop_running(self):
        bus = BroadcastEventDispatcher()
        seen: list[str] = []

        async def cb(tag=None, **kw):
            seen.append(tag)

        bus.on("x", cb)
        bus.emit_nowait("x", tag="a")
        await asyncio.sleep(0.05)
        assert seen == ["a"]

    def test_drops_without_running_loop(self):
        bus = BroadcastEventDispatcher()

        async def cb(**kw):
            pass

        bus.on("x", cb)
        bus.emit_nowait("x")  # no loop — must not raise


class TestDefaultBus:
    def test_singleton(self):
        assert get_bus() is get_bus()

    def test_set_bus_swaps(self):
        fresh = BroadcastEventDispatcher()
        set_bus(fresh)
        assert get_bus() is fresh


async def _noop() -> None:
    pass
