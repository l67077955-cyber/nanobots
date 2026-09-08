"""EndDiscussionTool stops writing engine._running (Phase 1 step 3).

The tool previously called
``lifecycle.mark_winding_down(..., flip_running=True)`` — a round-level
component writing the session-level flag — and its bare fallback (no
lifecycle wired) assigned ``engine._running = False`` directly. After step 3
the tool only transitions the lifecycle; the session verdict reaches
run_loop via ``broadcast_round``'s ``RoundResult.session_should_stop``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nanobot.groupchat.runtime.round_lifecycle import RoundLifecycle, RoundPhase
from nanobot.groupchat.runtime.tools.chatroom_tools import EndDiscussionTool


@pytest.mark.asyncio
async def test_end_discussion_marks_lifecycle_without_writing_engine_flag():
    evt = asyncio.Event()
    engine = SimpleNamespace(_running=True, _leader_end_reason="")
    lifecycle = RoundLifecycle(leader_end_event=evt, engine=engine)
    tool = EndDiscussionTool(end_event=evt, engine=engine, mailbox=None, lifecycle=lifecycle)

    out = await tool.execute(reason="信息已足够")

    # Lifecycle transition is intact…
    assert lifecycle.phase is RoundPhase.WINDING_DOWN
    assert lifecycle.reason == "leader_end_discussion"
    assert lifecycle.session_should_stop is True
    assert evt.is_set()  # legacy sentinel signal survives
    # …the engine records the reason for the unified termination notice…
    assert engine._leader_end_reason == "信息已足够"
    # …but the session-level flag is NOT written by the round-level tool.
    assert engine._running is True
    assert "讨论已结束" in out


@pytest.mark.asyncio
async def test_bare_end_discussion_fallback_keeps_end_event_only():
    """No lifecycle wired (bare tests / legacy path): end_event is still set,
    the engine flag is still untouched."""
    evt = asyncio.Event()
    engine = SimpleNamespace(_running=True, _leader_end_reason="")
    tool = EndDiscussionTool(end_event=evt, engine=engine, mailbox=None, lifecycle=None)

    await tool.execute(reason="")

    assert evt.is_set()
    assert engine._running is True
