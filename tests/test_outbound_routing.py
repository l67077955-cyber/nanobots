"""Tests for unified outbound routing via MessageBus."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.groupchat.runtime.engine import GroupChatEngine


def _minimal_engine() -> GroupChatEngine:
    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine._send_fn = None
    engine._send_outbound_fn = AsyncMock()
    engine._view_channel = "telegram"
    engine._view_chat_id = "42"
    engine._reply_channel = None
    engine._reply_chat_id = None
    return engine


@pytest.mark.asyncio
async def test_send_uses_bus_when_context_wired():
    engine = _minimal_engine()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "nanobot.groupchat.runtime.room_observability.emit_room_event",
            lambda **kwargs: None,
        )
        await engine._send("hello from agent")

    engine._send_outbound_fn.assert_awaited_once()
    msg = engine._send_outbound_fn.await_args.args[0]
    assert isinstance(msg, OutboundMessage)
    assert msg.channel == "telegram"
    assert msg.chat_id == "42"
    assert msg.content == "hello from agent"


@pytest.mark.asyncio
async def test_send_uses_pinned_reply_route_over_view_context():
    engine = _minimal_engine()
    engine._view_channel = "web"
    engine._view_chat_id = "dashboard"
    engine._reply_channel = "telegram"
    engine._reply_chat_id = "8008274300"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "nanobot.groupchat.runtime.room_observability.emit_room_event",
            lambda **kwargs: None,
        )
        await engine._send("pinned reply")

    msg = engine._send_outbound_fn.await_args.args[0]
    assert msg.channel == "telegram"
    assert msg.chat_id == "8008274300"


@pytest.mark.asyncio
async def test_set_tool_context_ignored_during_active_direct_chat():
    engine = _minimal_engine()
    engine._reply_channel = "telegram"
    engine._reply_chat_id = "42"
    engine._view_channel = "telegram"
    engine._view_chat_id = "42"

    async def _noop():
        pass

    engine._direct_chat_task = asyncio.create_task(_noop())
    engine.set_tool_context("web", "dashboard")
    assert engine._view_channel == "telegram"
    assert engine._view_chat_id == "42"
    engine._direct_chat_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await engine._direct_chat_task


@pytest.mark.asyncio
async def test_send_falls_back_to_legacy_send_fn():
    engine = _minimal_engine()
    engine._send_outbound_fn = None
    engine._send_fn = AsyncMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "nanobot.groupchat.runtime.room_observability.emit_room_event",
            lambda **kwargs: None,
        )
        await engine._send("legacy")

    engine._send_fn.assert_awaited_once_with("legacy")