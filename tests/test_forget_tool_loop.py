from __future__ import annotations

from typing import Any

import pytest

from nanobot.groupchat.orchestra.tools.tool_loop import tool_loop
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.tools.base import Tool
from nanobot.tools.forget import ForgetTool
from nanobot.tools.registry import ToolRegistry


class _FakeProvider(LLMProvider):
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
    ) -> LLMResponse:
        return self.responses.pop(0)


class _LargeTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "fake large read"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        }

    async def execute(self, *, path: str = "") -> str:
        return "large result " * 100


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_LargeTool())
    reg.register(ForgetTool())
    return reg


@pytest.mark.asyncio
async def test_forget_can_delete_current_mixed_tool_batch():
    messages = [{"role": "user", "content": "read and clean"}]
    provider = _FakeProvider([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest("read-1", "read_file", {"path": "big.txt"}),
                ToolCallRequest("forget-1", "forget", {"indices": 0}),
            ],
        ),
        LLMResponse(content="done"),
    ])

    result = await tool_loop(
        provider=provider,
        messages=messages,
        tool_registry=_registry(),
        model="fake",
        max_iterations=3,
    )

    assert result.content == "done"
    assert "read-1" in result.forgotten_tool_call_ids
    assert not any(
        msg.get("role") == "tool" and msg.get("tool_call_id") == "read-1"
        for msg in messages
    )
    assert any(
        msg.get("role") == "tool"
        and msg.get("tool_call_id") == "forget-1"
        and "read_file" in msg.get("content", "")
        for msg in messages
    )


@pytest.mark.asyncio
async def test_forget_alone_targets_previous_non_forget_batch():
    messages = [{"role": "user", "content": "read then clean"}]
    provider = _FakeProvider([
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("read-1", "read_file", {"path": "big.txt"})],
        ),
        LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("forget-1", "forget", {"indices": 0})],
        ),
        LLMResponse(content="done"),
    ])

    result = await tool_loop(
        provider=provider,
        messages=messages,
        tool_registry=_registry(),
        model="fake",
        max_iterations=4,
    )

    assert result.content == "done"
    assert "read-1" in result.forgotten_tool_call_ids
    assert not any(
        msg.get("role") == "tool" and msg.get("tool_call_id") == "read-1"
        for msg in messages
    )
