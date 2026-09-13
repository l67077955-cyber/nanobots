"""Regression: DeepSeek thinking models require reasoning_content on replay.

The official DeepSeek API rejects tool-loop replays whose assistant
``tool_calls`` messages lack ``reasoning_content``::

    400 "The reasoning_content in the thinking mode must be passed back to
    the API."

Reproduced 2026-09-12 by replaying a real failed gateway request (32 msgs,
4 tool iterations): original → 400, backfilled reasoning_content="" → 200.
Gateway routes (OpenRouter) strip reasoning server-side, so the backfill must
apply to DIRECT DeepSeek routes only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.state import settings_store


PM_PAYLOAD = {
    "providers": {
        "openrouter": {
            "url": "https://openrouter.ai/api/v1",
            "apiKey": "sk-or-v1-testgatewaykey",
        },
        "deepseek": {
            "url": "https://api.deepseek.com",
            "apiKey": "ds-test-key",
        },
        "zhipu": {
            "url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "apiKey": "zhipu-test-key",
        },
    },
    "models": {
        "openrouter": ["deepseek/deepseek-v4-pro"],
        "deepseek": ["deepseek-v4-pro"],
        "zhipu": ["glm-5.1"],
    },
}


@pytest.fixture()
def pm_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "providers_models.json"
    f.write_text(json.dumps(PM_PAYLOAD), encoding="utf-8")
    monkeypatch.setattr(settings_store, "PM_FILE", f)
    return f


def _tool_loop_messages() -> list[dict]:
    return [
        {"role": "user", "content": "查一下天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc1", "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "tc1", "content": "晴 25 度"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc2", "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "tc2", "content": "湿度 40%"},
    ]


def _provider() -> LiteLLMProvider:
    return LiteLLMProvider(api_key="sk-or-v1-testgatewaykey")


def test_deepseek_direct_route_backfills_reasoning_content(pm_file) -> None:
    p = _provider()
    msgs = _tool_loop_messages()
    kwargs = p._build_kwargs(msgs, model="deepseek-v4-pro", max_tokens=16)
    sent = kwargs["messages"]
    for m in sent:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            assert m.get("reasoning_content") == "", (
                "DeepSeek official API 400s when assistant tool_calls messages "
                "lack reasoning_content"
            )


def test_caller_messages_not_mutated(pm_file) -> None:
    p = _provider()
    msgs = _tool_loop_messages()
    p._build_kwargs(msgs, model="deepseek-v4-pro", max_tokens=16)
    for m in msgs:
        assert "reasoning_content" not in m, "copy-on-write violated: caller list mutated"


def test_existing_reasoning_content_preserved(pm_file) -> None:
    p = _provider()
    msgs = _tool_loop_messages()
    msgs[1]["reasoning_content"] = "real thinking"
    kwargs = p._build_kwargs(msgs, model="deepseek-v4-pro", max_tokens=16)
    sent = kwargs["messages"]
    assert sent[1]["reasoning_content"] == "real thinking"
    assert sent[3]["reasoning_content"] == ""


def test_non_deepseek_route_not_backfilled(pm_file) -> None:
    p = _provider()
    kwargs = p._build_kwargs(_tool_loop_messages(), model="glm-5.1", max_tokens=16)
    for m in kwargs["messages"]:
        if m.get("role") == "assistant":
            assert "reasoning_content" not in m, (
                "zhipu/glm route must not receive DeepSeek-specific fields"
            )


def test_gateway_route_not_backfilled(pm_file) -> None:
    """openrouter/deepseek-v4-pro (gateway prefix) → OpenRouter handles reasoning
    server-side; injecting the field may break strict providers downstream."""
    p = _provider()
    kwargs = p._build_kwargs(_tool_loop_messages(), model="deepseek/deepseek-v4-pro", max_tokens=16)
    for m in kwargs["messages"]:
        if m.get("role") == "assistant":
            assert "reasoning_content" not in m
