"""UI wait-feedback unit tests (no config)."""

from __future__ import annotations

import asyncio
import re
import time

import pytest

from nanobot.groupchat.display.status_tracker import AgentStatusTracker


@pytest.mark.asyncio
async def test_status_heartbeat_includes_seconds():
    edits: list[str] = []

    async def send_and_get_id(text: str) -> int:
        edits.append(text)
        return 1

    async def edit_fn(msg_id: int, text: str) -> None:
        edits.append(text)

    tracker = AgentStatusTracker(
        agents=["Kirk", "Harper"],
        leader="Kirk",
        edit_fn=edit_fn,
        send_and_get_id_fn=send_and_get_id,
    )
    tracker.HEARTBEAT_INTERVAL = 0.12
    tracker.EDIT_INTERVAL = 0.05

    await tracker.create_panel()
    await tracker.set_state("Kirk", "thinking", detail="glm")
    tracker._state_since["Kirk"] = time.monotonic() - 2.5
    tracker._dirty = True
    await tracker._maybe_refresh(force=True)
    await asyncio.sleep(0.35)
    tracker.stop_heartbeat()

    assert any("Kirk" in e for e in edits)
    assert any(re.search(r"\d+s", e) for e in edits), edits[-5:]


@pytest.mark.asyncio
async def test_render_does_not_crash():
    tracker = AgentStatusTracker(["A"], None, None, None)
    text = tracker._render()
    assert "status" in text
    assert "A" in text


@pytest.mark.asyncio
async def test_streaming_message_created_on_first_delta():
    """No pre-token placeholder: the stream message appears on first delta."""
    sent: list[str] = []

    async def send_and_get_id(text: str) -> int:
        sent.append(text)
        return 9

    async def edit_fn(msg_id: int, text: str) -> None:
        sent.append("edit:" + text)

    from nanobot.groupchat.display.streaming import StreamingDisplay

    stream = StreamingDisplay(
        "Kirk\n\n",
        send_and_get_id_fn=send_and_get_id,
        edit_fn=edit_fn,
    )
    assert stream.msg_id is None
    await stream.on_delta("hi")
    assert stream.msg_id == 9
    assert sent and "hi" in sent[0] and "▍" in sent[0]
