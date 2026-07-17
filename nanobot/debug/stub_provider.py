"""Stub LLM provider for dry-run debug sessions."""

from __future__ import annotations

import asyncio
from typing import Any

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest


class StubLLMProvider(LLMProvider):
    """Deterministic provider for offline debug and unit tests."""

    def __init__(
        self,
        *,
        responses: list[LLMResponse] | None = None,
        default_model: str = "stub/model",
        latency_s: float = 0.0,
        fail_transient_times: int = 0,
    ) -> None:
        super().__init__(api_key="stub", api_base=None, retry_delays=(0, 0, 0))
        self.generation = GenerationSettings(temperature=0.0, max_tokens=512)
        self.default_model = default_model
        self._queue: list[LLMResponse] = list(responses or [])
        self._latency_s = latency_s
        self._fail_transient_left = fail_transient_times
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return self.default_model

    def enqueue(self, *responses: LLMResponse) -> None:
        self._queue.extend(responses)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        sampling_params: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append({
            "model": model or self.default_model,
            "n_messages": len(messages),
            "n_tools": len(tools or []),
            "max_tokens": max_tokens,
        })
        if self._latency_s > 0:
            await asyncio.sleep(self._latency_s)

        if self._fail_transient_left > 0:
            self._fail_transient_left -= 1
            return LLMResponse(
                content="Error calling LLM: 503 overloaded (stub)",
                finish_reason="error",
                status_code=503,
            )

        if self._queue:
            return self._queue.pop(0)

        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                c = msg.get("content")
                last_user = c if isinstance(c, str) else str(c)
                break
        preview = (last_user or "(empty)").strip()[:200]
        return LLMResponse(
            content=f"[stub] {preview}",
            finish_reason="stop",
            usage={"prompt": 10, "completion": 5, "total": 15},
        )


def make_tool_call_response(
    name: str,
    arguments: dict[str, Any],
    *,
    content: str | None = None,
    call_id: str = "stub_tc_1",
) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )
