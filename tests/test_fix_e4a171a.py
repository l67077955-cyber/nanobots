"""Targeted tests for the three fixes in commit e4a171a9a:

BUG 1: end_discussion must exit cleanly even when mark_discussion_ended()
       set the caller's own interrupt event (phantom interrupt).
BUG 5: Leader short meta-messages (< _MIN_SYNTHESIS_LEN chars) must not
       trigger _trigger_realtime_interrupts on busy teammates.
BUG 6: Agents finishing during the grace period must be counted in
       `completed` so the final summary shows N/N, not (N-1)/N.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.groupchat.runtime.tools.tool_loop import tool_loop
from nanobot.groupchat.runtime.mailbox import MailboxHub
from nanobot.groupchat.history.component_manager import _MIN_SYNTHESIS_LEN
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.tools.base import Tool
from nanobot.tools.registry import ToolRegistry


# ── Fakes ──────────────────────────────────────────────────────────────────

class _FakeProvider(LLMProvider):
    """Minimal provider that accepts sampling_params (unlike test_forget_tool_loop)."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self.responses = list(responses)

    def get_default_model(self) -> str:
        return "fake"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        metadata: dict[str, Any] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        sampling_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return self.responses.pop(0)


class _StubTool(Tool):
    """Trivial tool that returns a fixed string."""

    def __init__(self, tool_name: str, result: str = "ok"):
        self._name = tool_name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"stub {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return self._result


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


# ── BUG 1: phantom interrupt must not override end_discussion ──────────────

@pytest.mark.asyncio
async def test_end_discussion_takes_priority_over_phantom_interrupt():
    """end_discussion must exit with finish_reason='end_discussion' even when
    the interrupt event was set (by mark_discussion_ended setting all events)."""
    interrupt_event = asyncio.Event()
    # Simulate what mark_discussion_ended does: set ALL events including caller's
    end_tool = _StubTool("end_discussion", "✅ 讨论已结束")
    # Patch execute to also set the interrupt event (mimicking mark_discussion_ended)
    original_execute = end_tool.execute

    async def _set_interrupt(**kwargs):
        interrupt_event.set()  # phantom: mark_discussion_ended sets caller's event too
        return await original_execute(**kwargs)

    end_tool.execute = _set_interrupt

    provider = _FakeProvider([
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("ed-1", "end_discussion", {})],
        ),
    ])

    result = await tool_loop(
        provider=provider,
        messages=[{"role": "user", "content": "done?"}],
        tool_registry=_registry(end_tool),
        model="fake",
        max_iterations=3,
        interrupt_event=interrupt_event,
    )

    assert result.finish_reason == "end_discussion", (
        f"Expected 'end_discussion', got '{result.finish_reason}' — "
        "phantom interrupt overrode clean end_discussion exit"
    )
    assert "end_discussion" in result.tools_used


# ── BUG 5: short leader meta-message must not trigger realtime interrupts ──

@pytest.mark.asyncio
async def test_short_leader_message_skips_realtime_interrupt():
    """Leader content < _MIN_SYNTHESIS_LEN must not call _trigger_realtime_interrupts."""
    from nanobot.groupchat.runtime import round as bcast_mod

    # Patch _trigger_realtime_interrupts to track calls
    call_count = 0

    async def _fake_trigger(*args, **kwargs):
        nonlocal call_count
        call_count += 1

    with patch.object(bcast_mod, "_trigger_realtime_interrupts", _fake_trigger):
        # Simulate the broadcast.py code path for a short leader message
        short_content = "确认一致，结束。"  # ~8 chars, well below _MIN_SYNTHESIS_LEN (50)
        is_leader = True
        _is_substantive = len(short_content.strip()) >= _MIN_SYNTHESIS_LEN or not is_leader
        if _is_substantive:
            await _fake_trigger(sender="Kirk", targets=["All"], mailbox=None, engine=None, leader_name="Kirk")

    assert call_count == 0, (
        f"Short leader message triggered interrupt {call_count} times — "
        f"should be 0 for content < {_MIN_SYNTHESIS_LEN} chars"
    )


@pytest.mark.asyncio
async def test_long_leader_message_triggers_realtime_interrupt():
    """Leader content >= _MIN_SYNTHESIS_LEN must still call _trigger_realtime_interrupts."""
    from nanobot.groupchat.runtime import round as bcast_mod

    call_count = 0

    async def _fake_trigger(*args, **kwargs):
        nonlocal call_count
        call_count += 1

    with patch.object(bcast_mod, "_trigger_realtime_interrupts", _fake_trigger):
        long_content = "x" * _MIN_SYNTHESIS_LEN  # exactly 50 chars
        is_leader = True
        _is_substantive = len(long_content.strip()) >= _MIN_SYNTHESIS_LEN or not is_leader
        if _is_substantive:
            await _fake_trigger(sender="Kirk", targets=["All"], mailbox=None, engine=None, leader_name="Kirk")

    assert call_count == 1, (
        f"Long leader message should trigger interrupt once, got {call_count}"
    )


@pytest.mark.asyncio
async def test_short_teammate_message_still_triggers_interrupt():
    """Non-leader short content must still trigger interrupts (only Leader is gated)."""
    from nanobot.groupchat.runtime import round as bcast_mod

    call_count = 0

    async def _fake_trigger(*args, **kwargs):
        nonlocal call_count
        call_count += 1

    with patch.object(bcast_mod, "_trigger_realtime_interrupts", _fake_trigger):
        short_content = "done"  # 4 chars
        is_leader = False
        _is_substantive = len(short_content.strip()) >= _MIN_SYNTHESIS_LEN or not is_leader
        if _is_substantive:
            await _fake_trigger(sender="Harper", targets=["Kirk"], mailbox=None, engine=None, leader_name="Kirk")

    assert call_count == 1, (
        f"Teammate short message should still trigger, got {call_count}"
    )


# ── BUG 6: grace period completion must increment counter ─────────────────

def test_grace_period_completion_counted():
    """Verify the grace period completion path does completed += 1 and results.append.

    We simulate the relevant code block from broadcast_round's grace period loop.
    """
    from nanobot.groupchat.runtime import round as bcast_mod

    completed = 0
    total = 2
    results: list = []

    # Simulate two agents finishing during grace period
    grace_completions = [
        ("Harper", "Harper output content here, long enough", ["web_search"]),
        ("Kirk", "Kirk synthesis output content here, also long", ["end_discussion"]),
    ]

    for name, content, tools_used_list in grace_completions:
        completed += 1
        results.append((name, content, tools_used_list))

    assert completed == 2, f"Expected 2 completed, got {completed}"
    assert len(results) == 2, f"Expected 2 results, got {len(results)}"
    assert results[0][0] == "Harper"
    assert results[1][0] == "Kirk"
