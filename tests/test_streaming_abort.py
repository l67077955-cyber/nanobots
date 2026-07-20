"""Tests for StreamingDisplay.abort() on /stop cancellation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.groupchat.display.streaming import StreamingDisplay
from nanobot.groupchat.runtime.engine import GroupChatEngine


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
async def test_on_reset_collapses_to_bare_wrench_marker():
    """April-May semantics: a tool call mid-stream collapses the in-progress
    message to a bare "🔧 ..." marker; the prelude is not kept on screen."""
    edited: list[tuple[int, str]] = []

    async def edit(msg_id: int, text: str) -> None:
        edited.append((msg_id, text))

    async def send(text: str) -> int:
        return 7

    stream = StreamingDisplay("💬 Harper:\n\n", send_and_get_id_fn=send, edit_fn=edit)
    # Simulate streamed prelude text
    await stream.on_delta("我来查一下")
    await stream.on_delta("那个文件")
    assert "我来查一下那个文件" in stream.buffer_text

    pre_edit_count = len(edited)
    await stream.on_reset()

    # The pre-tool message was edited exactly once on reset, collapsing to
    # the bare tool-in-progress marker (no prelude kept on screen).
    assert len(edited) == pre_edit_count + 1
    reset_text = edited[-1][1]
    assert reset_text == "💬 Harper:\n\n🔧 ..."

    # State: old message abandoned; next delta creates a new message below
    assert stream.msg_id is None
    assert stream._pre_tool_msg_id == 7
    assert stream.buffer_text == ""


@pytest.mark.asyncio
async def test_on_reset_uses_bare_placeholder_when_nothing_streamed():
    """When no text was streamed before the tool call, the bare "🔧 ..."
    placeholder is still used (nothing to preserve)."""
    edited: list[tuple[int, str]] = []

    async def edit(msg_id: int, text: str) -> None:
        edited.append((msg_id, text))

    stream = StreamingDisplay("💬 Harper:\n\n", edit_fn=edit)
    stream.msg_id = 5  # streaming message exists but buffer is empty
    await stream.on_reset()

    assert edited == [(5, "💬 Harper:\n\n🔧 ...")]
    assert stream._pre_tool_msg_id == 5


@pytest.mark.asyncio
async def test_finalize_collapses_pre_tool_message_to_down_marker():
    """April-May semantics: finalize() collapses the stale pre-tool message
    to a bare continued-below marker (header + "↓"), without the prelude."""
    edited: list[tuple[int, str]] = []
    _next_id = [10]

    async def edit(msg_id: int, text: str) -> None:
        edited.append((msg_id, text))

    async def send(text: str) -> int:
        _next_id[0] += 1
        return _next_id[0]

    stream = StreamingDisplay("💬 Harper:\n\n", send_and_get_id_fn=send, edit_fn=edit)
    await stream.on_delta("prelude text here")   # creates msg 11
    await stream.on_reset()                       # abandons msg 11 -> pre_tool
    pre_tool_id = stream._pre_tool_msg_id
    # Post-tool content streams into a NEW message (id 12)
    await stream.on_delta("final answer")
    await stream.finalize("final answer")

    # The pre-tool message was last edited to the bare continued-below marker
    pre_tool_edits = [t for mid, t in edited if mid == pre_tool_id]
    assert pre_tool_edits, "pre-tool message never edited"
    final_pre = pre_tool_edits[-1]
    assert final_pre == "💬 Harper:\n\n↓"


@pytest.mark.asyncio
async def test_direct_chat_aborts_stream_on_cancel():
    import importlib
    # runtime/__init__ re-exports direct_chat function, which shadows the submodule
    # on attribute access; load the real module via importlib.
    dc_mod = importlib.import_module("nanobot.groupchat.runtime.direct_chat")
    from nanobot.core.history import History

    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine._active_agents = ["Harper"]
    engine.registry = {"Harper": {"model": "test/model"}}
    engine.history = History()
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
        patch(
            "nanobot.groupchat.context.tool_log.build_tool_log",
            return_value="",
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await dc_mod.direct_chat(engine, "hello")

    engine._edit_fn.assert_awaited()
    final_text = engine._edit_fn.await_args.args[1]
    assert "⏹ 已中断" in final_text
    assert "▍" not in final_text


def test_clear_history_interrupts_active_turn():
    from nanobot.core.history import History
    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine.history = History()
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
    from nanobot.groupchat.runtime.direct_chat import direct_chat
    from nanobot.groupchat.context.prompt_builder import PromptBuilder
    from nanobot.core.history import History

    pb = PromptBuilder(config=GroupChatConfig(), workspace=Path("/tmp"))
    engine = SimpleNamespace(
        _active_agents=["Kirk"],
        registry={"Kirk": {"model": "test/model"}},
        history=History(),
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