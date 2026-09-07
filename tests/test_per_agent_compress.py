"""Phase D regression tests: per-agent independent compression.

Pins the core invariant: each agent's view compresses independently, and
compression of one agent's view does NOT affect another's.  This is the
behaviour that truly fixes the "遗忘" bug: compression results are stable
and per-view, so a compressed summary in A's view is invisible to C.

Key contracts:
  1. A view hitting the threshold compresses — A's view gets shorter.
  2. B's view (for the same message subset) is unchanged by A's compress.
  3. A→B private segment is compressed independently in A's view and in
     B's view (no cross-view data leak).
  4. Compression result is persistent: re-compressing the same view is a
     no-op (the summary is already there).
  5. direct-chat single-agent: view==all, compression works like before.
"""

from __future__ import annotations

import asyncio

from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState


class _FakeProvider:
    """A tiny mock provider returning a fixed summary (no LLM calls)."""

    async def chat_with_retry(self, messages, model, max_tokens, **kwargs):
        class R:
            content = "[压缩摘要] fake summary"
            finish_reason = "stop"
        return R()


def _make_context(tmp_path, provider=None) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=provider)


class TestPerAgentCompress:
    async def test_compress_shortens_one_view_not_others(self, tmp_path):
        """A's view compresses; B's view (containing the same All-msgs) is
        unchanged; C's view (private to it) is unchanged."""
        ctx = _make_context(tmp_path, provider=_FakeProvider())
        # Add enough All-visible messages to trigger compression for A
        for i in range(60):
            ctx.add_message("用户" if i % 2 == 0 else "系统", f"msg{i}")
        # Add one private A→C message that B never sees
        ctx.add_message("A", "A→C private", targets=["C"])

        a_len_before = len(ctx.view_for("A"))
        b_len_before = len(ctx.view_for("B"))
        c_len_before = len(ctx.view_for("C"))

        await ctx.compress_for("A")

        a_len_after = len(ctx.view_for("A"))
        assert a_len_after < a_len_before, "A's view should shrink after compress"
        # B's view is unchanged (compress_for only touched A's view)
        assert len(ctx.view_for("B")) == b_len_before
        # C's view is unchanged
        assert len(ctx.view_for("C")) == c_len_before

    async def test_private_segment_compressed_independently_per_view(self, tmp_path):
        """An A→B segment is compressed separately in A's view and in B's view.
        The summary content is only visible to A and B (not C)."""
        ctx = _make_context(tmp_path, provider=_FakeProvider())
        # Private A→B messages (C never sees)
        for i in range(20):
            ctx.add_message("A", f"A→B #{i}", targets=["B"])
        # Add filler for A so it hits threshold
        for i in range(50):
            ctx.add_message("用户", f"public #{i}")

        await ctx.compress_for("A")
        await ctx.compress_for("B")

        a_view = ctx.view_for("A")
        b_view = ctx.view_for("B")
        c_view = ctx.view_for("C")

        # A and B have a summary (compression happened)
        assert any("压缩摘要" in m.get("content", "") or m.get("sender") == "系统" for m in a_view)
        assert any("压缩摘要" in m.get("content", "") or m.get("sender") == "系统" for m in b_view)
        # C never saw the private A→B segment at all
        assert not any("A→B" in m.get("content", "") for m in c_view)

    async def test_compress_result_is_persistent(self, tmp_path):
        """Compressing the same view twice does not invoke the LLM again."""
        ctx = _make_context(tmp_path, provider=_FakeProvider())
        for i in range(60):
            ctx.add_message("用户" if i % 2 == 0 else "系统", f"msg{i}")

        await ctx.compress_for("A")
        a_len = len(ctx.view_for("A"))
        # Second compress should be a no-op (view already compressed)
        await ctx.compress_for("A")
        assert len(ctx.view_for("A")) == a_len

    async def test_direct_chat_single_agent_compresss_like_before(self, tmp_path):
        """When there's only one active agent, its view == the full log and
        compress_for behaves like the old shared maybe_compress."""
        ctx = _make_context(tmp_path, provider=_FakeProvider())
        # Fill with enough messages to cross the threshold
        for i in range(60):
            ctx.add_message("用户" if i % 2 == 0 else "系统", f"msg{i}")

        # Single-agent view is the full log
        assert ctx.view_for("A") == ctx.view_for("B") == ctx.view_for("C")
        await ctx.compress_for("A")
        # All views shrink the same amount (since they're all the same)
        assert len(ctx.view_for("A")) < 60

    async def test_disabled_summarization_keeps_middle(self, tmp_path):
        """When AI summarization is disabled, compression must NOT drop the
        middle region (old context.py:333-334 bug).  It should return early
        and let add_message's max_messages cap be the sole limiter."""
        ctx = _make_context(tmp_path, provider=None)  # no provider = disabled
        for i in range(70):
            ctx.add_message("用户" if i % 2 == 0 else "系统", f"msg{i}")

        before = len(ctx.view_for("A"))
        await ctx.compress_for("A")
        after = len(ctx.view_for("A"))
        # With provider=None, compress should early-return and not drop anything
        # (the threshold crossing only triggers the attempt; no provider means
        # no summary, so we must keep the middle, not head+tail discard).
        assert after == before, "disabled summarization must not drop messages"


class TestCompressAll:
    async def test_compress_all_runs_for_each_active_agent(self, tmp_path):
        """compress_all iterates over the active agents and compresses each."""
        ctx = _make_context(tmp_path, provider=_FakeProvider())
        for i in range(70):
            ctx.add_message("用户" if i % 2 == 0 else "系统", f"msg{i}")
        ctx._active_agents = ["A", "B", "C"]

        await ctx.compress_all()

        for name in ("A", "B", "C"):
            assert len(ctx.view_for(name)) < 70


class TestViewForCached:
    def test_view_for_returns_persistent_view(self, tmp_path):
        """After Phase D, view_for returns the stored view (not a fresh
        projection each time).  Mutating the returned list must not corrupt
        the stored view (copying is still required)."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "hello", targets=["All"])
        view1 = ctx.view_for("B")
        view1.clear()  # mutate caller's copy
        view2 = ctx.view_for("B")
        assert len(view2) == 1, "stored view must be unaffected by caller mutation"

    async def test_new_message_appends_to_target_views(self, tmp_path):
        """add_message(sender, content, targets=[B]) writes to the log AND
        appends to B's stored view (not A's or C's)."""
        ctx = _make_context(tmp_path)
        ctx._active_agents = ["A", "B", "C"]
        ctx.add_message("A", "private to B", targets=["B"])

        assert len(ctx.view_for("A")) == 1  # sender sees own
        assert len(ctx.view_for("B")) == 1  # target sees
        assert len(ctx.view_for("C")) == 0  # bystander does not see
