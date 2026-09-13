"""Regression: DeepSeek official API assistant-message validation rules.

Two replay-verified rules (2026-09-13, real failed gateway requests):

1. ``Invalid assistant message: content or tool_calls must be set``
   — assistant content=None is rejected. With tool_calls → ""; without
   → "(no text output)" (empty string without tool_calls still rejected).
2. ``The `reasoning_content` in the thinking mode must be passed back``
   — in thinking mode EVERY replayed assistant message must carry
   reasoning_content ("" suffices), including plain-text history messages.

Both are backfilled on DIRECT DeepSeek routes only — gateway routes
(OpenRouter) handle reasoning server-side and other providers may reject
the field.
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


def _mixed_history_messages() -> list[dict]:
    """Shape from the 2026-09-13 11:45 outage: group-chat history converted
    messages + tool loop with content=None assistants (thinking model)."""
    return [
        {"role": "user", "content": "群里聊过的内容"},
        {"role": "assistant", "content": "之前的纯文本回复"},  # plain history, no rc
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "tcA", "type": "function",
                         "function": {"name": "exec", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tcA", "content": "done"},
        {"role": "assistant", "content": None},  # thinking model: no text, no tools
    ]


def _provider() -> LiteLLMProvider:
    return LiteLLMProvider(api_key="sk-or-v1-testgatewaykey")


def test_deepseek_direct_backfills_tool_loop(pm_file) -> None:
    p = _provider()
    kwargs = p._build_kwargs(_tool_loop_messages(), model="deepseek-v4-pro", max_tokens=16)
    for m in kwargs["messages"]:
        if m.get("role") == "assistant":
            assert m.get("reasoning_content") == ""
            assert m.get("content") is not None


def test_deepseek_direct_fixes_mixed_history(pm_file) -> None:
    p = _provider()
    kwargs = p._build_kwargs(_mixed_history_messages(), model="deepseek-v4-pro", max_tokens=16)
    sent = kwargs["messages"]
    # plain-text history assistant gets rc=""
    assert sent[1]["reasoning_content"] == ""
    assert sent[1]["content"] == "之前的纯文本回复"
    # content=None + tool_calls → ""
    assert sent[3]["content"] == ""
    assert sent[3]["reasoning_content"] == ""
    # content=None without tool_calls → placeholder + rc
    assert sent[5]["content"] == "(no text output)"
    assert sent[5]["reasoning_content"] == ""


def test_caller_messages_not_mutated(pm_file) -> None:
    p = _provider()
    msgs = _mixed_history_messages()
    p._build_kwargs(msgs, model="deepseek-v4-pro", max_tokens=16)
    for m in msgs:
        assert "reasoning_content" not in m, "copy-on-write violated"
    assert msgs[3]["content"] is None
    assert msgs[5]["content"] is None


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
    kwargs = p._build_kwargs(_mixed_history_messages(), model="glm-5.1", max_tokens=16)
    for m in kwargs["messages"]:
        if m.get("role") == "assistant":
            assert "reasoning_content" not in m
            assert m.get("content") is None or isinstance(m.get("content"), str)


def test_gateway_route_not_backfilled(pm_file) -> None:
    p = _provider()
    kwargs = p._build_kwargs(_mixed_history_messages(), model="deepseek/deepseek-v4-pro", max_tokens=16)
    for m in kwargs["messages"]:
        if m.get("role") == "assistant":
            assert "reasoning_content" not in m
