"""Tests for StreamingDisplay.abort() on /stop cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.groupchat.display.streaming import StreamingDisplay
from nanobot.groupchat.orchestra.engine import GroupChatEngine


@pytest.mark.asyncio
async def test_streaming_display_abort_removes_cursor_and_marks_stopped():
    edited: list[tuple[int, str]] = []

    async def edit(msg_id: int, text: str) -> None:
        edited.append((msg_id, text))

    stream = StreamingDisplay("💬 Harper:\n\n", edit_fn=edit)
    stream.msg_id = 42
    stream._buffer = ["用户"]

    await stream.abort()

    assert stream.msg_id is None
    assert stream.buffer_text == ""
    assert edited == [(42, "💬 Harper:\n\n用户\n\n⏹ 已中断")]


@pytest.mark.asyncio
async def test_direct_chat_aborts_stream_on_cancel():
    from nanobot.groupchat.orchestra import direct_chat as dc_mod

    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine._active_agents = ["Harper"]
    engine.registry = {"Harper": {"model": "test/model"}}
    engine._history = []
    engine._view_channel = "telegram"
    engine._view_chat_id = "1"
    engine._direct_chat_queue = asyncio.Queue()
    engine._ensure_session_dir = lambda *a, **k: None
    engine._add_message = lambda *a, **k: None
    engine._maybe_compress_history = AsyncMock()
    engine._send = AsyncMock()
    engine._send_and_get_id_fn = AsyncMock(return_value=99)
    engine._edit_fn = AsyncMock()

    prompt_builder = AsyncMock()
    prompt_builder.build_single_agent_messages = lambda *_a, **_k: [{"role": "user", "content": "hi"}]
    engine._prompt_builder = prompt_builder

    async def cancelled_chat(**kwargs):
        delta = kwargs.get("on_content_delta")
        if delta:
            await delta("用户")
        raise asyncio.CancelledError()

    engine._chat_with_tools = cancelled_chat

    with (
        patch.object(dc_mod, "log_request"),
        patch.object(dc_mod, "build_tool_log", return_value=""),
    ):
        with pytest.raises(asyncio.CancelledError):
            await dc_mod.direct_chat(engine, "hello")

    engine._edit_fn.assert_awaited()
    final_text = engine._edit_fn.await_args.args[1]
    assert "⏹ 已中断" in final_text
    assert "▍" not in final_text


def test_clear_history_interrupts_active_turn():
    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine.history = type("H", (), {"clear": lambda self: None, "messages": []})()
    engine._history = []
    engine._request_log = []
    engine._active_stream = None
    engine._direct_chat_task = None
    engine.interrupt_active_turn = lambda **k: setattr(engine, "_interrupted", True)

    engine.clear_history()

    assert engine._interrupted is True


@pytest.mark.asyncio
async def test_direct_chat_skips_stream_callbacks_when_disabled():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nanobot.groupchat.config import GroupChatConfig
    from nanobot.groupchat.orchestra.direct_chat import direct_chat
    from nanobot.groupchat.history.prompt_builder import PromptBuilder

    pb = PromptBuilder(config=GroupChatConfig(), workspace=Path("/tmp"))
    engine = SimpleNamespace(
        _active_agents=["Kirk"],
        registry={"Kirk": {"model": "test/model"}},
        _history=[],
        _request_log=[],
        _view_channel="telegram",
        _view_chat_id="1",
        prompt_builder=pb,
        stream_replies=False,
        _ensure_session_dir=lambda *a, **k: None,
        _direct_chat_queue=asyncio.Queue(),
        _chat_with_tools=AsyncMock(return_value=("done", [], {"tokens": {}})),
        _add_message=lambda *a, **k: None,
        _maybe_compress_history=AsyncMock(),
        _send=AsyncMock(),
        _send_and_get_id_fn=AsyncMock(return_value=1),
        _edit_fn=AsyncMock(),
        register_active_stream=lambda *a, **k: None,
        clear_active_stream=lambda *a, **k: None,
    )

    await direct_chat(engine, "hello")

    kwargs = engine._chat_with_tools.await_args.kwargs
    assert kwargs.get("on_content_delta") is None
    assert kwargs.get("on_content_reset") is None


def test_add_agent_second_member_cancels_direct_chat():
    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine._active_agents = ["Harper"]
    engine.registry = {"Harper": {}, "Kirk": {}}
    engine._running = False
    engine._broadcast_tasks = {}
    engine._pending_join_queue = asyncio.Queue()
    engine._resolve_agent_name = lambda n: n if n in engine.registry else None
    engine._state = type("S", (), {"save_active": lambda *a: None})()
    engine.interrupt_active_turn = lambda **k: setattr(engine, "_interrupted", True)

    result = engine.add_agent("Kirk")

    assert engine._interrupted is True
    assert "Kirk" in result