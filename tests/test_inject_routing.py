"""Tests for inject() routing: 1 agent → direct_chat, 2+ → broadcast."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.groupchat.orchestra.engine import GroupChatEngine


def _minimal_engine(*, active: list[str]) -> GroupChatEngine:
    """Build an engine shell with only inject-routing state."""
    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine._active_agents = list(active)
    engine._running = False
    engine._direct_chat_task = None
    engine._direct_chat_queue = asyncio.Queue()
    engine._broadcast_tasks = {}
    engine._task = None
    engine._input_queue = asyncio.Queue()
    engine._view_channel = "telegram"
    engine._view_chat_id = "1"
    engine._active_stream = None
    return engine


@pytest.mark.asyncio
async def test_inject_single_agent_starts_direct_chat():
    engine = _minimal_engine(active=["Kirk"])
    engine.direct_chat = AsyncMock(return_value="reply")

    with patch("nanobot.groupchat.room_observability.emit_room_event"):
        engine.inject("hello")

    await asyncio.sleep(0.05)
    engine.direct_chat.assert_awaited_once_with("hello", media=None)
    assert not engine._running
    assert engine._direct_chat_task is None


@pytest.mark.asyncio
async def test_inject_single_agent_passes_media():
    engine = _minimal_engine(active=["Kirk"])
    engine.direct_chat = AsyncMock(return_value="reply")
    media = ["/tmp/photo.jpg"]

    with patch("nanobot.groupchat.room_observability.emit_room_event"):
        engine.inject("look at this", media=media)

    await asyncio.sleep(0.05)
    engine.direct_chat.assert_awaited_once_with("look at this", media=media)


@pytest.mark.asyncio
async def test_inject_single_agent_interjection_queues():
    engine = _minimal_engine(active=["Kirk"])

    async def slow_chat(_msg: str, *, media=None) -> str:
        await asyncio.sleep(5)
        return "done"

    engine.direct_chat = slow_chat
    with patch("nanobot.groupchat.room_observability.emit_room_event"):
        engine.inject("first")
    await asyncio.sleep(0.05)
    assert engine._direct_chat_task is not None

    with patch("nanobot.groupchat.room_observability.emit_room_event"):
        engine.inject("second", media=["/tmp/a.png"])
    assert engine._direct_chat_queue.get_nowait() == ("second", ["/tmp/a.png"])

    engine._cancel_direct_chat()


@pytest.mark.asyncio
async def test_inject_multi_agent_uses_broadcast_queue():
    engine = _minimal_engine(active=["Kirk", "Spock"])
    engine._start_group_loop = lambda: setattr(engine, "_running", True)

    with patch("nanobot.groupchat.room_observability.emit_room_event"):
        engine.inject("discuss this")

    assert engine._running
    assert engine._input_queue.get_nowait() == "discuss this"
    assert engine._direct_chat_task is None


@pytest.mark.asyncio
async def test_stop_cancels_direct_chat_task():
    engine = _minimal_engine(active=["Kirk"])

    async def slow_chat(_msg: str, *, media=None) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    engine.direct_chat = slow_chat
    with patch("nanobot.groupchat.room_observability.emit_room_event"):
        engine.inject("hello")
    await asyncio.sleep(0.05)
    assert engine._direct_chat_task is not None

    engine.stop()
    await asyncio.sleep(0.05)
    assert engine._direct_chat_task is None


@pytest.mark.asyncio
async def test_channel_manager_passes_media_to_inject():
    from nanobot.bus.events import InboundMessage
    from nanobot.channels.manager import ChannelManager
    from unittest.mock import MagicMock

    engine = _minimal_engine(active=["Kirk"])
    engine.inject = MagicMock()

    mgr = ChannelManager.__new__(ChannelManager)
    mgr._gc_engine = engine
    mgr.channels = {"telegram": MagicMock()}

    msg = InboundMessage(
        channel="telegram",
        sender_id="u1",
        chat_id="123",
        content="photo",
        media=["/tmp/x.jpg"],
    )
    await mgr._route_inbound(msg)
    engine.inject.assert_called_once_with("photo", media=["/tmp/x.jpg"])