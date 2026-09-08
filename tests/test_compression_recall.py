"""C2.2 (plan.md 批次 C2 第 2 条): memory-recall measurement for compression.

The industry case the plan cites — production memory recall 92% → 58% after
switching to summarised compression while blind quality reviews stayed flat
(silent degradation) — is the reason nanobot needs its own number.  These
tests produce it deterministically:

  * plant N machine-checkable facts (numbers / paths / decisions) at known
    positions of a real ``HistoryContext`` view;
  * run the real compressor with a fake provider whose summary keeps exactly
    the first *keep* planted facts verbatim and drops the rest
    (:class:`~compression_recall_metric.DeterministicSummaryProvider`);
  * measure recall over ``view_for(agent)`` with the reusable
    :func:`~compression_recall_metric.measure_recall`;
  * assert the metric computes **exactly** the expected value for the
    controlled drop — no absolute threshold is pinned anywhere.

Layout used throughout (settings monkeypatched: max_messages=100,
compress_ratio=0.5 → threshold 50; keep_recent=20; keep_user_messages=False,
so head = {0}):

    view length 60
    idx 0        head-protected opener (用户)
    idx 1..39    middle region the compressor replaces   (8 facts planted)
    idx 40..59   keep_recent tail                        (2 facts planted)

Hence, for a summary that keeps the first *k* of the 8 middle facts:

    recall(summarise, k) = (2 + min(k, 8)) / 10      — exact
    recall(truncate)     = 2 / 10                    — the tail-only floor

The truncation route (tool_pruning style: drop the oldest unprotected
messages, keep protected head + recent tail, **no LLM call**) is simulated
locally in ``_truncate_deterministically`` — nanobot/ is untouched (C2 is a
只测不改 batch).
"""

from __future__ import annotations

import pytest

from compression_recall_metric import (
    DeterministicSummaryProvider,
    Fact,
    measure_recall,
)
from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.runtime.events import BroadcastEventDispatcher, get_bus, set_bus

# ── The planted fact set ──────────────────────────────────────────────────
# 8 facts in the compressible middle (order == view order — the provider
# keeps "the first k of these"), 2 facts in the protected tail.

MIDDLE_FACTS: tuple[Fact, ...] = (
    Fact("budget", "本季度预算上限是 47300 元", "47300 元"),
    Fact("config_path", "网桥配置文件在 /etc/nanobot/bridge.yaml", "/etc/nanobot/bridge.yaml"),
    Fact("api_port", "对外 API 端口定为 8443", "端口定为 8443"),
    Fact("batch_size", "迁移批处理大小定为 256 条", "批处理大小定为 256"),
    Fact("owner", "迁移负责人是 Chen Wei", "Chen Wei"),
    Fact("deadline", "上线截止日期是 2026-10-15", "2026-10-15"),
    Fact("rollback_branch", "回滚分支保留为 revert/bridge-v3", "revert/bridge-v3"),
    Fact("decision_queue", "最终决定采用消息队列方案", "消息队列方案"),
)

TAIL_FACTS: tuple[Fact, ...] = (
    Fact("alert_email", "告警邮箱设为 ops@nanobot.dev", "ops@nanobot.dev"),
    Fact("log_level", "日志级别定为 WARNING", "日志级别定为 WARNING"),
)

FACTS: tuple[Fact, ...] = MIDDLE_FACTS + TAIL_FACTS

# ── Layout constants (must match the docstring diagram) ───────────────────

_TOTAL_MESSAGES = 60
_MIDDLE_FACT_INDICES = (3, 7, 12, 17, 25, 28, 33, 38)  # inside 1..39
_TAIL_FACT_INDICES = (45, 57)  # inside 40..59
_MIDDLE_SIZE = 39  # indices 1..39 — what the compressor replaces
_HEAD_MODEL = "openai/test-recall-summarizer"


def expected_summary_recall(keep: int) -> float:
    """Exact expected recall after summarising with the first *keep* kept."""
    return (len(TAIL_FACTS) + min(keep, len(MIDDLE_FACTS))) / len(FACTS)


def _make_context(tmp_path, provider=None) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=provider)


def _seed_history(ctx: HistoryContext, *, user_fact_ids: frozenset[str] = frozenset()) -> None:
    """Seed the 60-message view described in the module docstring.

    *user_fact_ids* turns the given planted facts into 用户-sender messages
    (for head-protection experiments); everything else is filler that never
    contains a fact needle.
    """
    fact_at: dict[int, Fact] = {
        **dict(zip(_MIDDLE_FACT_INDICES, MIDDLE_FACTS)),
        **dict(zip(_TAIL_FACT_INDICES, TAIL_FACTS)),
    }
    for i in range(_TOTAL_MESSAGES):
        fact = fact_at.get(i)
        if i == 0:
            ctx.add_message("用户", "会话开始:网桥迁移项目启动。")
        elif fact is not None:
            sender = "用户" if fact.id in user_fact_ids else "系统"
            ctx.add_message(sender, fact.planted_content())
        else:
            ctx.add_message("系统", f"常规进度同步 {i:02d}:各模块运行平稳。")


def _truncate_deterministically(
    view: list[dict], *, keep_recent: int = 20, keep_users: bool = False
) -> list[dict]:
    """tool_pruning-style deterministic truncation, simulated in-test only.

    Same protection semantics as ``_compress_view`` (head: first message +
    user messages when *keep_users*; tail: last *keep_recent*) but the
    unprotected middle is **dropped outright** — no LLM, no summary.  This
    is the "直接丢最旧消息" alternative route the plan wants compared
    against summarisation on the same fact set with the same metric.
    """
    total = len(view)
    head_idx = HistoryContext._find_head_indices(view, keep_all_users=keep_users)
    tail_idx = set(range(max(0, total - keep_recent), total))
    protected = head_idx | tail_idx
    return [dict(m) for i, m in enumerate(view) if i in protected]


@pytest.fixture(autouse=True)
def _compression_window(monkeypatch):
    """Small deterministic window: threshold 50, tail 20, head {0}."""
    monkeypatch.setattr(history_settings, "max_messages", lambda: 100)
    monkeypatch.setattr(history_settings, "max_context_chars", lambda: 0)
    monkeypatch.setattr(history_settings, "compress_ratio", lambda: 0.5)
    monkeypatch.setattr(history_settings, "compression_keep_recent", lambda: 20)
    monkeypatch.setattr(history_settings, "keep_user_messages", lambda: False)
    monkeypatch.setattr(history_settings, "history_summarize_enabled", lambda: True)
    monkeypatch.setattr(history_settings, "compress_max_summary_tokens", lambda: 600)
    monkeypatch.setattr(history_settings, "history_summarize_model", lambda: _HEAD_MODEL)
    monkeypatch.setattr(history_settings, "summarize_model", lambda: "openai/fallback-summarizer")


@pytest.fixture(autouse=True)
def _isolated_bus():
    set_bus(BroadcastEventDispatcher())
    yield
    set_bus(BroadcastEventDispatcher())


@pytest.fixture()
def compressed_events() -> list[dict]:
    events: list[dict] = []

    async def _rec(**kw):
        events.append(kw)

    get_bus().on("history:compressed", _rec)
    return events


# ── 1. The metric itself computes exactly what it is given ────────────────


class TestRecallMetric:
    def test_all_facts_present_recall_is_one(self):
        msgs = [{"sender": "系统", "content": f.planted_content()} for f in FACTS]
        report = measure_recall(FACTS, msgs)
        assert report.recall == 1.0
        assert report.misses == ()
        assert set(report.hits) == {f.id for f in FACTS}

    def test_no_facts_present_recall_is_zero(self):
        msgs = [{"sender": "系统", "content": "常规进度同步 01:各模块运行平稳。"}]
        report = measure_recall(FACTS, msgs)
        assert report.recall == 0.0
        assert report.hits == ()
        assert set(report.misses) == {f.id for f in FACTS}

    def test_partial_drop_gives_exact_fraction_and_detail(self):
        """Drop 5 of 10 facts → recall exactly 0.5 with exact hit/miss ids."""
        kept, dropped = FACTS[:5], FACTS[5:]
        msgs = [{"sender": "系统", "content": f.planted_content()} for f in kept]
        report = measure_recall(FACTS, msgs)
        assert report.recall == 0.5 == 5 / 10
        assert report.hits == tuple(f.id for f in kept)
        assert report.misses == tuple(f.id for f in dropped)

    def test_fact_surviving_only_inside_summary_counts_as_hit(self):
        """The needle living inside a summary message (not the original) is
        still a recall hit — that is the whole premise of summarised
        compression being measured fairly."""
        summary_msg = {
            "sender": "系统",
            "content": "[早期对话摘要(压缩了 39 条中间消息)]\n事实 FACT[budget]: "
            "本季度预算上限是 47300 元",
        }
        report = measure_recall(FACTS[:1], [summary_msg])
        assert report.recall == 1.0
        assert report.hits == ("budget",)

    def test_needle_split_across_messages_is_not_a_hit(self):
        """A needle must be retrievable within ONE message — fragments
        split across messages do not count (原文检索 semantics)."""
        fact = FACTS[0]
        needle = fact.needle
        msgs = [
            {"sender": "系统", "content": f"前半 {needle[: len(needle) // 2]}"},
            {"sender": "系统", "content": f"{needle[len(needle) // 2 :]} 后半"},
        ]
        assert measure_recall([fact], msgs).recall == 0.0

    def test_empty_fact_set_is_vacuously_full_recall(self):
        assert measure_recall([], []).recall == 1.0

    def test_duplicate_fact_ids_are_rejected(self):
        fact = FACTS[0]
        with pytest.raises(ValueError, match="duplicate fact id"):
            measure_recall([fact, fact], [{"content": "x"}])

    def test_missing_content_keys_are_tolerated(self):
        report = measure_recall(FACTS[:1], [{}, {"content": FACTS[0].planted_content()}])
        assert report.recall == 1.0


# ── 2. Recall around the real compressor (standalone compress_for) ───────


class TestRecallAcrossCompression:
    async def test_recall_is_full_before_compression(self, tmp_path):
        """Baseline: the seeded view recalls all 10 planted facts — any loss
        measured afterwards is attributable to the route, not the seeding."""
        ctx = _make_context(tmp_path, provider=DeterministicSummaryProvider(keep=3))
        ctx.set_active_agents(["A"])
        _seed_history(ctx)

        report = measure_recall(FACTS, ctx.view_for("A"))

        assert len(ctx.view_for("A")) == _TOTAL_MESSAGES
        assert report.recall == 1.0
        assert len(report.hits) == 10

    async def test_summary_keeping_first_k_facts_gives_exact_recall(self, tmp_path):
        """keep=3 → the first 3 middle facts survive inside the summary, the
        other 5 middle facts are gone, both tail facts survive in place:
        recall = (2 + 3) / 10 = 0.5 exactly."""
        provider = DeterministicSummaryProvider(keep=3)
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A"])
        _seed_history(ctx)

        await ctx.compress_for("A")

        assert len(provider.calls) == 1, "standalone compress_for: one LLM call"
        report = measure_recall(FACTS, ctx.view_for("A"))
        assert report.recall == (2 + 3) / 10 == 0.5
        assert report.hits == ("budget", "config_path", "api_port", "alert_email", "log_level")
        assert report.misses == (
            "batch_size",
            "owner",
            "deadline",
            "rollback_branch",
            "decision_queue",
        )

    async def test_compression_region_matches_the_layout(self, tmp_path, compressed_events):
        """Cross-check the measured region against the history:compressed
        event: 39 middle messages dropped, 60 → 22 (1 head + 1 summary + 20
        tail).  The recall numbers above are about exactly this region."""
        provider = DeterministicSummaryProvider(keep=3)
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A"])
        _seed_history(ctx)

        await ctx.compress_for("A")

        assert len(compressed_events) == 1
        event = compressed_events[0]
        assert event["agent"] == "A"
        assert event["dropped"] == _MIDDLE_SIZE
        assert event["view_before"] == _TOTAL_MESSAGES
        assert event["view_after"] == 1 + 1 + 20
        assert event["model"] == _HEAD_MODEL

    async def test_summary_resolves_model_via_history_summarize_model(self, tmp_path):
        """C1.3: the compression call asks for history_summarize_model()'s
        value (not the tool_results fallback) — pinned from the provider's
        view so the recall experiment's only variable is *keep*."""
        provider = DeterministicSummaryProvider(keep=1)
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A"])
        _seed_history(ctx)

        await ctx.compress_for("A")

        assert provider.calls[0]["model"] == history_settings.history_summarize_model()
        assert provider.calls[0]["model"] == _HEAD_MODEL
        assert provider.calls[0]["metadata"]["log_mode"] == "history_compress"

    async def test_user_message_fact_survives_via_head_protection(self, tmp_path, monkeypatch):
        """keep_user_messages=True lifts ALL user messages into head
        protection: the 'owner' fact planted as a 用户 message survives even
        when the summary keeps nothing (keep=0) → recall (2 + 1) / 10."""
        monkeypatch.setattr(history_settings, "keep_user_messages", lambda: True)
        provider = DeterministicSummaryProvider(keep=0)
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A"])
        _seed_history(ctx, user_fact_ids=frozenset({"owner"}))

        await ctx.compress_for("A")

        report = measure_recall(FACTS, ctx.view_for("A"))
        assert report.recall == (2 + 1) / 10 == 0.3
        assert "owner" in report.hits

    async def test_compress_all_dedup_preserves_recall_for_both_agents(self, tmp_path):
        """Through compress_all (C1.2: identical views share ONE provider
        call) both agents' views receive the same summary → identical
        recall. keep=2 → (2 + 2) / 10 = 0.4 for each."""
        provider = DeterministicSummaryProvider(keep=2)
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A", "B"])
        _seed_history(ctx)

        await ctx.compress_all()

        assert len(provider.calls) == 1, "identical views share one summary call (C1.2)"
        report_a = measure_recall(FACTS, ctx.view_for("A"))
        report_b = measure_recall(FACTS, ctx.view_for("B"))
        assert report_a.recall == report_b.recall == (2 + 2) / 10 == 0.4
        assert report_a.hits == report_b.hits


# ── 3. Summarisation vs deterministic truncation, side by side ────────────


class TestRouteComparison:
    async def test_truncation_route_needs_no_llm_and_loses_all_middle_facts(self, tmp_path):
        """Dropping the same middle region outright (tool_pruning style)
        recalls only the 2 tail facts — 0.2 — with zero provider calls."""
        provider = DeterministicSummaryProvider(keep=3)  # would keep 3; must never run
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A"])
        _seed_history(ctx)

        truncated = _truncate_deterministically(ctx.view_for("A"))

        assert provider.calls == [], "deterministic truncation must not call the LLM"
        assert len(truncated) == 1 + 20, "head + tail only, no summary placeholder"
        assert _TOTAL_MESSAGES - len(truncated) == _MIDDLE_SIZE, "same region the compressor replaces"
        report = measure_recall(FACTS, truncated)
        assert report.recall == 2 / 10 == 0.2
        assert report.hits == ("alert_email", "log_level")
        assert len(report.misses) == 8

    @pytest.mark.parametrize("keep", [0, 1, 3, 8, 12])
    async def test_summary_vs_truncation_side_by_side(self, tmp_path, keep):
        """Both routes drop exactly the same 39 middle messages on the same
        fact set; the only difference is what replaces them.  The metric
        must return exactly:

            summarise(keep=k) = (2 + min(k, 8)) / 10   — one LLM call
            truncate          = 2 / 10                 — zero LLM calls

        A summary that keeps nothing (k=0) is exactly as lossy as blind
        truncation; from k=1 up, summarisation strictly dominates on
        recall while paying one summary call (the C2.4 cost dimension).
        """
        # — route 1: the real HistoryContext summarisation path —
        provider = DeterministicSummaryProvider(keep=keep)
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A"])
        _seed_history(ctx)
        await ctx.compress_for("A")

        summ_report = measure_recall(FACTS, ctx.view_for("A"))

        # — route 2: deterministic truncation of the same region —
        provider2 = DeterministicSummaryProvider(keep=keep)
        ctx2 = _make_context(tmp_path, provider=provider2)
        ctx2.set_active_agents(["A"])
        _seed_history(ctx2)
        trunc_report = measure_recall(
            FACTS, _truncate_deterministically(ctx2.view_for("A"))
        )

        assert summ_report.recall == expected_summary_recall(keep)
        assert trunc_report.recall == 2 / 10
        assert trunc_report.recall <= summ_report.recall
        if keep == 0:
            assert summ_report.recall == trunc_report.recall, (
                "a summary keeping nothing is exactly blind truncation"
            )
        elif keep >= len(MIDDLE_FACTS):
            assert summ_report.recall == 1.0, "keeping every middle fact restores full recall"
        else:
            assert summ_report.recall > trunc_report.recall

        # Cost dimension of the comparison: 1 summary call vs none.
        assert len(provider.calls) == 1
        assert provider2.calls == []

        # Both routes shed the same 39 source messages.
        assert len(ctx.view_for("A")) == _TOTAL_MESSAGES - _MIDDLE_SIZE + 1  # + summary
        assert summ_report.hits == (
            tuple(f.id for f in MIDDLE_FACTS[: min(keep, len(MIDDLE_FACTS))])
            + tuple(f.id for f in TAIL_FACTS)
        )
