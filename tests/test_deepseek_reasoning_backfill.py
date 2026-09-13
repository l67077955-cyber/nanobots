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


def test_deepseek_orphan_tool_calls_get_synthetic_results(pm_file) -> None:
    """Interrupted tool loops leave assistant(tool_calls) without following
    tool results at the history tail — DeepSeek 400s on replay
    ("must be followed by tool messages"). Backfill a synthetic result."""
    p = _provider()
    msgs = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "",
         "reasoning_content": "",
         "tool_calls": [
             {"id": "tcX", "type": "function",
              "function": {"name": "exec", "arguments": "{}"}},
             {"id": "tcY", "type": "function",
              "function": {"name": "web_search", "arguments": "{}"}},
         ]},
        # interrupt: neither tcX nor tcY got a tool result; a new user turn follows
        {"role": "user", "content": "算了不用了"},
    ]
    kwargs = p._build_kwargs(msgs, model="deepseek-v4-pro", max_tokens=16)
    sent = kwargs["messages"]
    # sanitize normalizes tool_call ids (9-char hash) — compare against the
    # ids the assistant message actually carries, not the input literals.
    idx = next(i for i, m in enumerate(sent) if m.get("tool_calls"))
    expected_ids = {tc["id"] for tc in sent[idx]["tool_calls"]}
    tool_msgs = [m for m in sent if m.get("role") == "tool"]
    assert {m.get("tool_call_id") for m in tool_msgs} == expected_ids
    assert all("(tool call interrupted" in m.get("content", "") for m in tool_msgs)
    # synthetic results sit directly after the assistant(tool_calls) message
    assert sent[idx + 1]["role"] == "tool" and sent[idx + 2]["role"] == "tool"


def test_deepseek_partial_orphan_backfills_only_missing(pm_file) -> None:
    """If one of two tool calls got its result before the interrupt, only the
    missing one is backfilled and the existing one stays in place."""
    p = _provider()
    msgs = [
        {"role": "user", "content": "查一下"},
        {"role": "assistant", "content": "", "reasoning_content": "",
         "tool_calls": [
             {"id": "tcA", "type": "function",
              "function": {"name": "exec", "arguments": "{}"}},
             {"id": "tcB", "type": "function",
              "function": {"name": "exec", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": "tcA", "content": "done"},
        {"role": "user", "content": "被打断了"},
    ]
    kwargs = p._build_kwargs(msgs, model="deepseek-v4-pro", max_tokens=16)
    sent = kwargs["messages"]
    a = next(m for m in sent if m.get("tool_calls"))
    ids = [tc["id"] for tc in a["tool_calls"]]  # normalized ids, in order
    tool_msgs = [m for m in sent if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    by_id = {m.get("tool_call_id"): m for m in tool_msgs}
    assert set(by_id) == set(ids)
    existing, missing = ids[0], ids[1]
    assert by_id[existing]["content"] == "done"
    assert "(tool call interrupted" in by_id[missing]["content"]


def test_deepseek_orphan_tool_results_dropped(pm_file) -> None:
    """Rule 4: a tool message whose id has no preceding assistant(tool_calls)
    declaration (interrupt cut the assistant away, kept the result) is
    rejected by DeepSeek — dropped on direct routes."""
    p = _provider()
    msgs = [
        {"role": "user", "content": "查一下"},
        # orphan result: its assistant(tool_calls) was lost to an interrupt
        {"role": "tool", "tool_call_id": "orphan1", "content": "Successfully wrote 92 bytes"},
        {"role": "assistant", "content": "", "reasoning_content": "",
         "tool_calls": [{"id": "tcZ", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tcZ", "content": "file content"},
        {"role": "user", "content": "继续"},
    ]
    kwargs = p._build_kwargs(msgs, model="deepseek-v4-pro", max_tokens=16)
    sent = kwargs["messages"]
    tool_msgs = [m for m in sent if m.get("role") == "tool"]
    ids = {m.get("tool_call_id") for m in tool_msgs}
    orphan_real = next(m for m in sent if m.get("tool_calls"))["tool_calls"][0]["id"]
    assert orphan_real in ids           # declared pair kept (normalized id)
    assert len(tool_msgs) == 1          # orphan dropped
    # caller list untouched
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "orphan1" for m in msgs)


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
