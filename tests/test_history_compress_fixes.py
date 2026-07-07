"""Regression tests for history compression fixes.

Covers:
1. Race-safety: messages appended during maybe_compress's LLM await survive
2. Prior summary blocks are protected from re-compression
3. AI-summarize failure falls back to mechanical compression (not drop)
4. result_processor head+tail sampling
5. add_message early-exit does not break over-budget trimming
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.groupchat.history.context import (
    HistoryContext,
    _SUMMARY_PREFIX,
    find_last_compact_boundary,
    is_compact_summary,
)


class _FakeState:
    def save_message(self, sender, content, messages):
        pass


def _ctx(provider=None) -> HistoryContext:
    return HistoryContext(state=_FakeState(), provider=provider)


def _fill(ctx: HistoryContext, n: int, start: int = 0) -> None:
    ctx.messages.append({"sender": "系统", "content": "system prompt"})
    ctx.messages.append({"sender": "用户", "content": "user question"})
    for i in range(start, start + n):
        ctx.messages.append({"sender": "AgentA", "content": f"agent message {i} " + "x" * 50})


_SETTINGS_PATCH = {
    "max_messages": lambda: 20,
    "history_summarize_enabled": lambda: True,
    "summarize_model": lambda: "fake/model",
    "compress_ratio": lambda: 0.8,
    "compress_max_summary_tokens": lambda: 500,
    "compression_keep_recent": lambda: 4,
    "keep_user_messages": lambda: False,
    "get_context_window_tokens": lambda: 1_000_000,
    # New knobs introduced with the compaction cleanup. Patched so the tests
    # do not depend on the real ~/.nanobot/history_settings.json on the host.
    "token_trigger_ratio": lambda: 0.55,
    "context_budget_ratio": lambda: 0.65,
    "compress_fallback_chars": lambda: 2000,
    "cross_turn_repeat_guard": lambda: True,
    "cross_turn_repeat_ratio": lambda: 0.85,
}


def _patch_settings():
    import nanobot.groupchat.history.history_settings as hs
    patches = [patch.object(hs, name, fn) for name, fn in _SETTINGS_PATCH.items()]
    return patches


@pytest.mark.asyncio
async def test_message_added_during_compress_await_survives():
    """A message appended by add_message during the LLM await must not be dropped."""
    ctx = _ctx()

    async def slow_chat(**kwargs):
        # Simulate a message arriving mid-summarisation
        ctx.add_message("AgentB", "LATE MESSAGE arriving during compression")
        class R:
            content = "摘要内容"
        return R()

    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(side_effect=slow_chat)
    ctx._provider = provider

    _fill(ctx, 30)  # 32 msgs > 0.8 * 20 triggers compression
    patches = _patch_settings()
    for p in patches:
        p.start()
    try:
        await ctx.maybe_compress()
    finally:
        for p in patches:
            p.stop()

    contents = [m["content"] for m in ctx.messages]
    assert any("LATE MESSAGE" in c for c in contents), "message added during await was dropped"
    assert any(_SUMMARY_PREFIX in c for c in contents), "summary not injected"


@pytest.mark.asyncio
async def test_prior_summary_block_protected_on_second_compress():
    """A summary injected by a previous pass must survive the next pass."""
    provider = AsyncMock()
    class R:
        content = "第二次摘要"
    provider.chat_with_retry = AsyncMock(return_value=R())

    ctx = _ctx(provider)
    ctx.messages.append({"sender": "系统", "content": "system prompt"})
    ctx.messages.append({"sender": "用户", "content": "user question"})
    old_summary = f"{_SUMMARY_PREFIX}（压缩了 10 条中间消息）]\n重要的历史结论 XYZ"
    ctx.messages.append({"sender": "系统", "content": old_summary})
    for i in range(30):
        ctx.messages.append({"sender": "AgentA", "content": f"filler {i} " + "y" * 50})

    patches = _patch_settings()
    for p in patches:
        p.start()
    try:
        await ctx.maybe_compress()
    finally:
        for p in patches:
            p.stop()

    contents = [m["content"] for m in ctx.messages]
    assert any("重要的历史结论 XYZ" in c for c in contents), "prior summary was lost"


@pytest.mark.asyncio
async def test_ai_failure_falls_back_to_mechanical_compress():
    """When the LLM call fails, middle must be mechanically compressed, not dropped."""
    provider = AsyncMock()
    provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("boom"))

    ctx = _ctx(provider)
    _fill(ctx, 30)
    patches = _patch_settings()
    for p in patches:
        p.start()
    try:
        await ctx.maybe_compress()
    finally:
        for p in patches:
            p.stop()

    contents = [m["content"] for m in ctx.messages]
    assert any(c.startswith("[早期对话压缩") for c in contents), (
        "mechanical compress block missing — middle was silently dropped"
    )
    # some gist of the middle should survive inside the block
    block = next(c for c in contents if c.startswith("[早期对话压缩"))
    assert "agent message" in block


def test_head_tail_sample():
    from nanobot.groupchat.history.result_processor import _head_tail_sample

    text = "HEAD" + "a" * 10_000 + "TAIL_ERROR_HERE"
    sample = _head_tail_sample(text, 1000)
    assert len(sample) < 1200  # budget + elision marker
    assert sample.startswith("HEAD")
    assert sample.endswith("TAIL_ERROR_HERE")
    assert "elided" in sample

    short = "short text"
    assert _head_tail_sample(short, 1000) == short


def test_add_message_still_trims_when_over_budget():
    """Early-exit must not disable trimming for genuinely over-budget histories."""
    import nanobot.groupchat.history.history_settings as hs

    ctx = _ctx()
    # window 1000 tokens -> budget 650 tokens; content far exceeds it
    with (
        patch.object(hs, "max_messages", lambda: 500),
        patch.object(hs, "keep_user_messages", lambda: False),
        patch.object(hs, "get_context_window_tokens", lambda: 1000),
    ):
        ctx.messages.append({"sender": "系统", "content": "sys"})
        ctx.messages.append({"sender": "用户", "content": "q"})
        for i in range(20):
            ctx.add_message("AgentA", f"msg {i} " + "z" * 500)

    total_chars = sum(len(m["content"]) for m in ctx.messages)
    # 20 * ~507 chars = ~10k chars ≈ 2.5k+ tokens >> 650 budget; must have trimmed
    assert total_chars < 10_000, f"no trimming happened ({total_chars} chars kept)"


def test_add_message_early_exit_keeps_small_history_intact():
    import nanobot.groupchat.history.history_settings as hs

    ctx = _ctx()
    with (
        patch.object(hs, "max_messages", lambda: 500),
        patch.object(hs, "keep_user_messages", lambda: False),
        patch.object(hs, "get_context_window_tokens", lambda: 1_000_000),
    ):
        for i in range(10):
            ctx.add_message("AgentA", f"small message {i}")

    assert len(ctx.messages) == 10
    assert all("small message" in m["content"] for m in ctx.messages)


# ──────────────────────────────────────────────────────────────────────────
# Cross-turn repetition guard (observational, log-only)
# ──────────────────────────────────────────────────────────────────────────


def test_cross_turn_similarity_basics():
    from nanobot.groupchat.history.repetition import (
        cross_turn_similarity,
        is_cross_turn_repeat,
    )

    body = "这是一段足够长的 agent 回复，用来越过最短长度阈值，" * 4
    near_dup = body + " 末尾略加几个字。"
    different = "完全不同的另一段话题内容，讨论的是另一个方向，" * 4

    # Near-duplicate → high similarity, flagged
    score = cross_turn_similarity(near_dup, body)
    assert score >= 0.85, score
    rep, s = is_cross_turn_repeat(near_dup, body, 0.85)
    assert rep and s >= 0.85

    # Genuinely different → low similarity, not flagged
    rep2, s2 = is_cross_turn_repeat(different, body, 0.85)
    assert not rep2 and s2 < 0.5, s2

    # Too short → never flagged (avoids "ok"/"done" false positives)
    rep3, s3 = is_cross_turn_repeat("ok", "ok", 0.85)
    assert not rep3 and s3 == 0.0


def test_add_message_warns_on_cross_turn_repeat(monkeypatch):
    """An agent restating its previous turn near-verbatim logs a WARNING."""
    import nanobot.groupchat.history.context as ctx_mod
    import nanobot.groupchat.history.history_settings as hs

    warnings: list[str] = []

    def _capture_warning(msg, *args, **kwargs):
        warnings.append(str(msg))

    monkeypatch.setattr(ctx_mod.logger, "warning", _capture_warning)
    monkeypatch.setattr(hs, "cross_turn_repeat_guard", lambda: True)
    monkeypatch.setattr(hs, "cross_turn_repeat_ratio", lambda: 0.85)

    ctx = _ctx()
    body = "Harper 的诊断结论：P0 瓶颈是 BuildOutput 每帧全量重建，" * 5
    ctx.add_message("Harper", body)
    # Near-verbatim restatement (whitespace + a small tail diff)
    ctx.add_message("Harper", body + "\n\n完全接受，结论收敛。")

    assert any("cross-turn repeat" in w for w in warnings), (
        f"expected cross-turn repeat warning, got: {warnings}"
    )
    # Content is NOT mutated (log-only guard)
    assert ctx.messages[-1]["content"].startswith(body)


def test_add_message_no_warn_for_different_agents_or_short(monkeypatch):
    """No warning when the prior same-sender message is absent, short, or different."""
    import nanobot.groupchat.history.context as ctx_mod
    import nanobot.groupchat.history.history_settings as hs

    warnings: list[str] = []
    monkeypatch.setattr(ctx_mod.logger, "warning", lambda *a, **k: warnings.append(str(a[0])))
    monkeypatch.setattr(hs, "cross_turn_repeat_guard", lambda: True)
    monkeypatch.setattr(hs, "cross_turn_repeat_ratio", lambda: 0.85)

    ctx = _ctx()
    ctx.add_message("Harper", "short one")  # too short → no prior, no warn
    ctx.add_message("Kirk", "short two")     # different agent, short → no warn
    long_a = "一段足够长的不同内容来描述另一个话题的方向，" * 5
    long_b = "完全不一样的方向，另一组关键词和论证，" * 5
    ctx.add_message("Harper", long_a)        # first long Harper msg → no prior long
    ctx.add_message("Harper", long_b)        # different content → low similarity

    assert not any("cross-turn repeat" in w for w in warnings), warnings





def _tool_log_msg(idx: int, preview_len: int = 150) -> dict:
    """Build an assistant message whose content carries a <previous_tool_calls>
    block with a preview of *preview_len* chars (age_tool_log truncates to 100)."""
    preview = "p" * preview_len
    return {
        "sender": "AgentA",
        "content": (
            f"agent reply {idx}\n"
            "<previous_tool_calls>\n"
            f"• exec(command=\"ls\") → {preview} (1,234字)\n"
            "</previous_tool_calls>"
        ),
    }


def _patch_microcompact_settings(keep_recent: int = 4):
    import nanobot.groupchat.history.history_settings as hs
    return [patch.object(hs, "compression_keep_recent", lambda: keep_recent)]


def test_microcompact_ages_old_tool_logs():
    """Tool-log blocks older than keep_recent get their preview truncated to 100 chars."""
    ctx = _ctx()
    ctx.messages.append({"sender": "系统", "content": "system prompt"})
    ctx.messages.append({"sender": "用户", "content": "user question"})
    # 6 tool-log messages, keep_recent=4 → first 2 are eligible (idx 2,3)
    for i in range(6):
        ctx.messages.append(_tool_log_msg(i))

    for p in _patch_microcompact_settings(keep_recent=4):
        p.start()
    try:
        ctx.microcompact()
    finally:
        for p in _patch_microcompact_settings(keep_recent=4):
            p.stop()

    # Eligible old blocks (idx 2, 3 → messages[2], messages[3]) were aged
    aged_preview = "p" * 100
    assert f"→ {aged_preview} (1,234字)" in ctx.messages[2]["content"]
    assert f"→ {aged_preview} (1,234字)" in ctx.messages[3]["content"]
    # Tail (last 4) untouched: still 150-char preview
    full_preview = "p" * 150
    assert f"→ {full_preview} (1,234字)" in ctx.messages[4]["content"]
    assert f"→ {full_preview} (1,234字)" in ctx.messages[7]["content"]


def test_microcompact_idempotent():
    """Running microcompact twice yields identical content."""
    ctx = _ctx()
    ctx.messages.append({"sender": "系统", "content": "sys"})
    for i in range(6):
        ctx.messages.append(_tool_log_msg(i))

    patches = _patch_microcompact_settings(keep_recent=4)
    for p in patches:
        p.start()
    try:
        ctx.microcompact()
        after_first = [m["content"] for m in ctx.messages]
        ctx.microcompact()
        after_second = [m["content"] for m in ctx.messages]
    finally:
        for p in patches:
            p.stop()

    assert after_first == after_second


def test_microcompact_does_not_touch_tail_or_head():
    """Index 0 (system) and last keep_recent messages are never mutated."""
    ctx = _ctx()
    ctx.messages.append({"sender": "系统", "content": "system prompt"})
    ctx.messages.append({"sender": "用户", "content": "user question"})
    for i in range(6):
        ctx.messages.append(_tool_log_msg(i))

    sys_before = ctx.messages[0]["content"]
    user_before = ctx.messages[1]["content"]
    tail_before = [m["content"] for m in ctx.messages[-4:]]

    patches = _patch_microcompact_settings(keep_recent=4)
    for p in patches:
        p.start()
    try:
        ctx.microcompact()
    finally:
        for p in patches:
            p.stop()

    assert ctx.messages[0]["content"] == sys_before
    assert ctx.messages[1]["content"] == user_before
    assert [m["content"] for m in ctx.messages[-4:]] == tail_before


def test_microcompact_skipped_while_compress_active():
    """When _compress_active is True, microcompact must not mutate anything."""
    ctx = _ctx()
    ctx.messages.append({"sender": "系统", "content": "sys"})
    for i in range(6):
        ctx.messages.append(_tool_log_msg(i))

    before = [m["content"] for m in ctx.messages]
    ctx._compress_active = True
    patches = _patch_microcompact_settings(keep_recent=4)
    for p in patches:
        p.start()
    try:
        ctx.microcompact()
    finally:
        for p in patches:
            p.stop()
    ctx._compress_active = False

    assert [m["content"] for m in ctx.messages] == before


# ──────────────────────────────────────────────────────────────────────────
# Phase 2-B: structured compact boundary marker
# ──────────────────────────────────────────────────────────────────────────


def test_is_compact_summary_flag_and_legacy_prefix():
    """Predicate recognises both the structured flag and the legacy prefix.

    Note: the predicate is sender-agnostic — it inspects only the flag and the
    content prefix. The ``sender == '系统'`` guard is applied at the call site
    in maybe_compress, so an agent message that happens to start with the
    prefix is still reported as a boundary here (and filtered out upstream).
    """
    assert is_compact_summary({"sender": "系统", "content": "x", "is_compact_summary": True})
    assert is_compact_summary({"sender": "系统", "content": "[早期对话摘要（压缩了 3 条）]\n..."})
    assert is_compact_summary({"sender": "系统", "content": "   [早期对话压缩（N 条）]\n..."})
    # Plain message (no flag, no prefix) is not a boundary
    assert not is_compact_summary({"sender": "AgentA", "content": "normal agent reply"})
    assert not is_compact_summary({"sender": "系统", "content": "plain system msg"})


def test_find_last_compact_boundary():
    msgs = [
        {"sender": "系统", "content": "sys"},
        {"sender": "系统", "content": "[早期对话摘要（压缩了 1 条）]\ns1", "is_compact_summary": True},
        {"sender": "AgentA", "content": "a1"},
        {"sender": "系统", "content": "[早期对话压缩（2 条）]\ns2", "is_compact_summary": True},
        {"sender": "AgentA", "content": "a2"},
    ]
    assert find_last_compact_boundary(msgs) == 3
    assert find_last_compact_boundary(msgs[:2]) == 1
    assert find_last_compact_boundary([msgs[0], msgs[2], msgs[4]]) == -1


@pytest.mark.asyncio
async def test_structured_flag_protects_summary_without_prefix():
    """A summary carrying is_compact_summary=True (no string prefix) survives compression."""
    provider = AsyncMock()

    class R:
        content = "新的摘要"
    provider.chat_with_retry = AsyncMock(return_value=R())

    ctx = _ctx(provider)
    ctx.messages.append({"sender": "系统", "content": "system prompt"})
    ctx.messages.append({"sender": "用户", "content": "user question"})
    # Prior summary recognised ONLY by the structured flag (no legacy prefix)
    ctx.messages.append({
        "sender": "系统",
        "content": "CUSTOM_FLAG_SUMMARY_BODY_ZZZ",
        "is_compact_summary": True,
    })
    for i in range(30):
        ctx.messages.append({"sender": "AgentA", "content": f"filler {i} " + "y" * 50})

    patches = _patch_settings()
    for p in patches:
        p.start()
    try:
        await ctx.maybe_compress()
    finally:
        for p in patches:
            p.stop()

    contents = [m["content"] for m in ctx.messages]
    assert any("CUSTOM_FLAG_SUMMARY_BODY_ZZZ" in c for c in contents), (
        "structured-flag summary without prefix was not protected"
    )


@pytest.mark.asyncio
async def test_legacy_prefix_summary_still_protected():
    """Backward compat: an old summary without the flag survives via the prefix fallback."""
    provider = AsyncMock()

    class R:
        content = "又一次摘要"
    provider.chat_with_retry = AsyncMock(return_value=R())

    ctx = _ctx(provider)
    ctx.messages.append({"sender": "系统", "content": "system prompt"})
    ctx.messages.append({"sender": "用户", "content": "user question"})
    # Legacy summary: prefix present, NO structured flag (as persisted by old versions)
    ctx.messages.append({
        "sender": "系统",
        "content": f"{_SUMMARY_PREFIX}（压缩了 5 条中间消息）]\nLEGACY_BODY_XXX",
    })
    for i in range(30):
        ctx.messages.append({"sender": "AgentA", "content": f"filler {i} " + "z" * 50})

    patches = _patch_settings()
    for p in patches:
        p.start()
    try:
        await ctx.maybe_compress()
    finally:
        for p in patches:
            p.stop()

    contents = [m["content"] for m in ctx.messages]
    assert any("LEGACY_BODY_XXX" in c for c in contents), "legacy prefix summary was not protected"

