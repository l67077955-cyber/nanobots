"""C2.4 (plan.md 批次 C2 第 4 条): 摘要压缩 vs 确定性截断,并排对照实验。

C2.2 (tests/test_compression_recall.py, commit 375ca771b) 已经在同一
60 消息视图 / 同一事实集上钉住了**召回维度**: 摘要路线 recall =
(2 + min(k, 8)) / 10 随摘要保留量 k 单调上升,截断路线恒 2 / 10。
本文件补上对照的另外两个维度,它们在既有测试里没有被钉住:

  * **下轮 prompt 足迹** (成本维度的 char 空间代理): 截断删掉整个中段,
    摘要路线留下 head + 摘要 + tail——两者之差恰好等于摘要消息自身的
    字符数 ("常驻摘要税")。char 是 token 的一阶代理 (±CJK 分词差异),
    方向性结论与 token 空间一致。
  * **缓存前缀发散点**: 两条路线与原视图的最长公共消息前缀都只有
    head 保护的 1 条——重写点同为第一个非保护消息。因此"换成截断
    就能救回 prompt 缓存命中"不成立;缓存友好需要的是把重写点后移
    (plan C3 选项 1 的领域,tool_pruning.prune_conversation_tail_with_summary
    已有"摘要追加进最后一条 system 消息"的先例,其缓存收益同样未度量)。

召回锚点复测: 第一组测试在本文件自己的布局上重新推导 C2.2 的两个
锚点数字 (keep=3 → 0.5; 截断 → 0.2),两个文件因此无法静默漂移。

事实集与布局与 test_compression_recall.py 逐字相同 (Deterministic-
SummaryProvider 的输出按 FACT 标签顺序决定,可比性依赖这一点):
threshold 50, keep_recent 20, head={0}, 60 条视图, 8 事实在中段
idx 1..39, 2 事实在尾部 idx 40..59。只测不改: nanobot/ 零改动。
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
from nanobot.groupchat.runtime.events import BroadcastEventDispatcher, set_bus

# ── Fact set: verbatim copy of test_compression_recall.py ─────────────────
# (docstring explains why the copy is deliberate, not drift)

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

_TOTAL_MESSAGES = 60
_MIDDLE_FACT_INDICES = (3, 7, 12, 17, 25, 28, 33, 38)  # inside 1..39
_TAIL_FACT_INDICES = (45, 57)  # inside 40..59
_MIDDLE_START, _MIDDLE_END = 1, 40  # the region the compressor replaces
_SUMMARY_PREFIX = "[早期对话摘要"


def _make_context(tmp_path, provider=None) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=provider)


def _seed_history(ctx: HistoryContext) -> None:
    """The 60-message view described in the module docstring (C2.2 layout)."""
    fact_at: dict[int, Fact] = {
        **dict(zip(_MIDDLE_FACT_INDICES, MIDDLE_FACTS)),
        **dict(zip(_TAIL_FACT_INDICES, TAIL_FACTS)),
    }
    for i in range(_TOTAL_MESSAGES):
        fact = fact_at.get(i)
        if i == 0:
            ctx.add_message("用户", "会话开始:网桥迁移项目启动。")
        elif fact is not None:
            ctx.add_message("系统", fact.planted_content())
        else:
            ctx.add_message("系统", f"常规进度同步 {i:02d}:各模块运行平稳。")


def _truncate_deterministically(
    view: list[dict], *, keep_recent: int = 20
) -> list[dict]:
    """tool_pruning-style deterministic truncation, simulated in-test only.

    Same protection semantics as ``_compress_view`` (head: first message;
    tail: last *keep_recent*) but the unprotected middle is dropped outright
    — no LLM, no summary message inserted.
    """
    total = len(view)
    head_idx = HistoryContext._find_head_indices(view, keep_all_users=False)
    tail_idx = set(range(max(0, total - keep_recent), total))
    protected = head_idx | tail_idx
    return [dict(m) for i, m in enumerate(view) if i in protected]


def _chars(view: list[dict]) -> int:
    """Total content chars of a view — the char-space prompt-footprint proxy."""
    return sum(len(str(m.get("content", ""))) for m in view)


def _identity(m: dict) -> tuple:
    return (m.get("sender"), m.get("content"))


def _common_prefix_len(a: list[dict], b: list[dict]) -> int:
    """Length of the longest identical leading (sender, content) sequence.

    A prefix cache keyed on the exact prompt prefix survives exactly this far
    (message granularity — the honest floor; char-level divergence can only
    be at the same position or later).
    """
    n = 0
    for x, y in zip(a, b):
        if _identity(x) != _identity(y):
            break
        n += 1
    return n


def _summary_message(view: list[dict]) -> dict:
    for m in view:
        if str(m.get("content", "")).startswith(_SUMMARY_PREFIX):
            return m
    raise AssertionError(f"no summary message in view: {[m.get('content', '')[:20] for m in view]}")


@pytest.fixture(autouse=True)
def _compression_window(monkeypatch):
    """Same deterministic window as C2.2: threshold 50, tail 20, head {0}."""
    monkeypatch.setattr(history_settings, "max_messages", lambda: 100)
    monkeypatch.setattr(history_settings, "max_context_chars", lambda: 0)
    monkeypatch.setattr(history_settings, "compress_ratio", lambda: 0.5)
    monkeypatch.setattr(history_settings, "compression_keep_recent", lambda: 20)
    monkeypatch.setattr(history_settings, "keep_user_messages", lambda: False)
    monkeypatch.setattr(history_settings, "history_summarize_enabled", lambda: True)
    monkeypatch.setattr(history_settings, "compress_max_summary_tokens", lambda: 600)
    monkeypatch.setattr(history_settings, "history_summarize_model", lambda: "openai/test-recall-summarizer")
    monkeypatch.setattr(history_settings, "summarize_model", lambda: "openai/fallback-summarizer")


@pytest.fixture(autouse=True)
def _isolated_bus():
    set_bus(BroadcastEventDispatcher())
    yield
    set_bus(BroadcastEventDispatcher())


async def _summarised_view(tmp_path, keep: int) -> tuple[list[dict], DeterministicSummaryProvider]:
    """Seed + run the real HistoryContext compression route; return the view."""
    provider = DeterministicSummaryProvider(keep=keep)
    ctx = _make_context(tmp_path, provider=provider)
    ctx.set_active_agents(["A"])
    _seed_history(ctx)
    await ctx.compress_for("A")
    return ctx.view_for("A"), provider


def _original_view(tmp_path) -> list[dict]:
    ctx = _make_context(tmp_path)
    ctx.set_active_agents(["A"])
    _seed_history(ctx)
    return ctx.view_for("A")


# ── 1. Paradigm consistency with C2.2's published recall numbers ───────────


class TestParadigmConsistency:
    async def test_layout_reproduces_c22_recall_anchors(self, tmp_path):
        """This file's layout must reproduce test_compression_recall.py's
        anchors exactly: summarise(keep=3) → 0.5, truncate → 0.2, one LLM
        call vs zero.  If either file's layout drifts, this fails and the
        numbers quoted in docs/compression-vs-cache-2026-09.md go stale."""
        view, provider = await _summarised_view(tmp_path, keep=3)
        assert len(provider.calls) == 1
        assert measure_recall(FACTS, view).recall == (2 + 3) / 10 == 0.5

        truncated = _truncate_deterministically(_original_view(tmp_path))
        assert measure_recall(FACTS, truncated).recall == 2 / 10 == 0.2


# ── 2. Next-prompt footprint (cost dimension, char-space proxy) ───────────


class TestPromptFootprint:
    @pytest.mark.parametrize("keep", [0, 3, 8])
    async def test_next_prompt_savings_per_route_are_exact(
        self, tmp_path, keep: int
    ):
        """Both routes shed the same 39 middle messages; what differs is the
        replacement.  In chars, exactly:

            savings(truncate)   = chars(middle)                  — all of it
            savings(summarise)  = chars(middle) − chars(summary)  — keeps S

        so the truncation route always shrinks the NEXT prompt strictly more,
        by exactly the summary message's own size (the standing summary tax).
        """
        original = _original_view(tmp_path)
        middle_chars = _chars(original[_MIDDLE_START:_MIDDLE_END])

        summ_view, provider = await _summarised_view(tmp_path, keep=keep)
        assert len(provider.calls) == 1
        summary_msg = _summary_message(summ_view)
        trunc_view = _truncate_deterministically(original)

        savings_summ = _chars(original) - _chars(summ_view)
        savings_trunc = _chars(original) - _chars(trunc_view)

        assert savings_summ == middle_chars - len(summary_msg["content"])
        assert savings_trunc == middle_chars
        assert savings_trunc - savings_summ == len(summary_msg["content"]) > 0
        assert savings_summ > 0, "a summary smaller than the middle still nets positive here"

    async def test_summary_tax_is_positive_even_when_summary_keeps_nothing(self, tmp_path):
        """keep=0: the summary contains zero planted facts yet still occupies
        prompt chars — the tax floor.  (Production analogue: analyzer events
        with S > M, where the tax exceeds the whole middle — 5/431 events,
        see docs/compression-vs-cache-2026-09.md §4.)"""
        original = _original_view(tmp_path)
        summ_view, _ = await _summarised_view(tmp_path, keep=0)
        truncated = _truncate_deterministically(original)

        summary_msg = _summary_message(summ_view)
        assert "没有保留任何具体事实" in summary_msg["content"]
        assert len(summary_msg["content"]) > 0
        assert _chars(truncated) < _chars(summ_view), (
            "even a fact-free summary leaves the next prompt bigger than truncation"
        )
        assert measure_recall(FACTS, summ_view).recall == measure_recall(
            FACTS, truncated
        ).recall == 0.2, "and at keep=0 both routes are equally blind"


# ── 3. Cache-prefix divergence (cache dimension) ──────────────────────────


class TestPrefixDivergence:
    async def test_summarisation_diverges_at_first_unprotected_message(self, tmp_path):
        """After the real compression route, the view shares only the head
        message (index 0) with the original — the rewrite point is the FIRST
        unprotected message, so the whole post-head cached prefix is gone."""
        original = _original_view(tmp_path)
        summ_view, _ = await _summarised_view(tmp_path, keep=3)

        assert _common_prefix_len(original, summ_view) == 1

    async def test_truncation_diverges_at_the_same_first_unprotected_message(self, tmp_path):
        """Deterministic truncation does NOT preserve the post-head prefix
        either: dropping the middle shifts every later message's position, so
        its common prefix with the original is likewise just the head.  The
        cache dimension therefore cannot discriminate between the two routes
        as implemented — being "deterministic" buys zero cache friendliness."""
        original = _original_view(tmp_path)
        truncated = _truncate_deterministically(original)

        summ_view, _ = await _summarised_view(tmp_path, keep=3)
        assert _common_prefix_len(original, truncated) == 1
        assert _common_prefix_len(original, truncated) == _common_prefix_len(
            original, summ_view
        ), "both routes break the prefix at the same point"

    async def test_protected_regions_survive_byte_identical_in_both_routes(self, tmp_path):
        """What neither route touches: the head message and the 20 tail
        messages survive byte-identical (sender + content).  The routes
        differ ONLY in what replaces the middle — which is why the only
        cache-relevant lever is WHERE the rewrite happens (C3 option 1),
        not which of these two routes is chosen."""
        original = _original_view(tmp_path)
        protected = [_identity(m) for m in (original[:1] + original[_MIDDLE_END:])]

        summ_view, _ = await _summarised_view(tmp_path, keep=3)
        trunc_view = _truncate_deterministically(original)

        for view in (summ_view, trunc_view):
            identities = [_identity(m) for m in view if not str(m.get("content", "")).startswith(_SUMMARY_PREFIX)]
            assert identities == protected
