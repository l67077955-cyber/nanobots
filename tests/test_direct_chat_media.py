"""Tests for direct_chat multimodal message building."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.runtime.direct import direct_chat
from nanobot.groupchat.history.prompt_builder import PromptBuilder


def _png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00"
        b"\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


@pytest.mark.asyncio
async def test_direct_chat_builds_multimodal_user_message(tmp_path):
    from nanobot.core.history import History
    img = tmp_path / "shot.png"
    img.write_bytes(_png_bytes())

    pb = PromptBuilder(config=GroupChatConfig(), workspace=tmp_path)
    engine = SimpleNamespace(
        _active_agents=["Kirk"],
        registry={"Kirk": {"model": "test/model"}},
        _session_dir=tmp_path / "session",
        history=History(),
        _topic="",
        _view_channel="telegram",
        _view_chat_id="99",
        _request_log=[],
        prompt_builder=pb,
        _save_event=lambda *a, **k: None,
        _ensure_session_dir=lambda *a, **k: None,
        _direct_chat_queue=asyncio.Queue(),
        _chat_with_tools=AsyncMock(return_value=("ok", [], {"tokens": {}})),
        _add_message=lambda *a, **k: None,
        _maybe_compress_history=AsyncMock(),
        _send=AsyncMock(),
        _send_and_get_id_fn=None,
        _edit_fn=None,
    )

    with patch("nanobot.groupchat.runtime.direct.StreamingDisplay") as mock_stream:
        inst = mock_stream.return_value
        inst.enabled = False
        inst.finalize = AsyncMock()
        await direct_chat(engine, "describe", media=[str(img)])

    call_messages = engine._chat_with_tools.await_args.kwargs["messages"]
    user_msg = call_messages[-1]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list)
    assert any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for block in user_msg["content"]
    )