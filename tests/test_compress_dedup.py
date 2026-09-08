"""C1.2 (plan.md 批次 C1 第 2 条): dedup identical summary calls per batch.

``compress_all`` compresses every active agent's view; most messages target
``All`` and land synchronously in every view, so N identical views each fired
their own — content-identical — summary LLM call in the same round.  These
tests pin that within ONE ``compress_all`` batch, views whose (middle-region
per-message content + summary params) match share a single provider call and
the resulting summary, while:

  * the privacy invariant is untouched: an A→B private segment makes the
    views' hashes differ (each compresses on its own), and C's view NEVER
    contains a summary covering content C could not see;
  * a single-message difference is a different group;
  * standalone ``compress_for`` calls never share (scope = one batch);
  * every view still emits its own ``history:compressed`` event — N events,
    1 provider call for an identical-view group.
"""

from __future__ import annotations

import hashlib

import pytest

from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.runtime.events import BroadcastEventDispatcher, get_bus, set_bus


class _HashProvider:
    """Deterministic summaries derived from the prompt.

    The returned content embeds a digest of the prompt, so two calls with
    identical input are observably identical and two calls with different
    input are observably different.
    """

    def __init__(self, content: str | None = "[unused]") -> None:
        self.calls: list[dict] = []
        self._content = content

    async def chat_with_retry(self, messages, model=None, max_tokens=None,
                              metadata=None, **kwargs):
        prompt = messages[0]["content"]
        self.calls.append({"prompt": prompt, "model": model,
                           "max_tokens": max_tokens, "metadata": metadata})
        if self._content is None:
            raise RuntimeError("provider down")

        class _R:
            pass

        r = _R()
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        r.content = self._content if "[digest]" not in self._content else (
            self._content.replace("[digest]", digest)
        )
        r.finish_reason = "stop"
        r.usage = {"prompt_tokens": 100, "completion_tokens": 20}
        r.cost = 0.004
        return r


def _make_context(tmp_path, provider=None) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=provider)


@pytest.fixture(autouse=True)
def _small_compression_window(monkeypatch):
    monkeypatch.setattr(history_settings, "max_messages", lambda: 50)
    monkeypatch.setattr(history_settings, "max_context_chars", lambda: 0)
    monkeypatch.setattr(history_settings, "compress_ratio", lambda: 0.8)
    monkeypatch.setattr(history_settings, "compression_keep_recent", lambda: 6)
    monkeypatch.setattr(history_settings, "keep_user_messages", lambda: False)
    monkeypatch.setattr(history_settings, "history_summarize_enabled", lambda: True)
    monkeypatch.setattr(history_settings, "compress_max_summary_tokens", lambda: 600)
    monkeypatch.setattr(history_settings, "summarize_model", lambda: "openai/test-summarizer")


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


def _summaries(view: list[dict]) -> list[str]:
    return [m["content"] for m in view if m.get("sender") == "系统" and "压缩" in m.get("content", "")]


class TestIdenticalViewsShareOneCall:
    async def test_one_provider_call_and_event_per_view(self, tmp_path, compressed_events):
        """Three identical All-only views → 1 provider call, 3 events, every
        view ends up holding the same summary."""
        provider = _HashProvider(content="[压缩摘要 digest]")
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A", "B", "C"])
        for i in range(60):
            ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")

        await ctx.compress_all()

        assert len(provider.calls) == 1, "identical views must share one LLM call"
        assert {e["agent"] for e in compressed_events} == {"A", "B", "C"}, (
            "each view still emits its own history:compressed event"
        )
        assert all(e["model"] == "openai/test-summarizer" for e in compressed_events)
        a_sum = _summaries(ctx.view_for("A"))
        assert len(a_sum) == 1
        assert _summaries(ctx.view_for("B")) == a_sum
        assert _summaries(ctx.view_for("C")) == a_sum

    async def test_shared_event_payloads_carry_response_telemetry(self, tmp_path, compressed_events):
        """The N events of a shared group all describe the one real LLM call
        (tokens/cost come off that shared response)."""
        provider = _HashProvider(content="[压缩摘要 digest]")
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A", "B"])
        for i in range(60):
            ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")

        await ctx.compress_all()

        assert len(compressed_events) == 2
        for e in compressed_events:
            assert e["prompt_tokens"] == 100
            assert e["completion_tokens"] == 20
            assert e["cost"] == 0.004
            assert e["dropped"] > 0
            assert e["view_after"] < e["view_before"]


class TestPrivacyInvariant:
    async def test_private_segment_compresses_per_group_and_stays_private(self, tmp_path):
        """A→B private messages put A's and B's views in one group (both saw
        them) and C's in another (public only) → 2 calls; C's view never
        contains the private content NOR the summary that covers it."""
        provider = _HashProvider(content="[压缩摘要 digest]")
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A", "B", "C"])
        for i in range(40):
            ctx.add_message("用户" if i == 0 else "系统", f"public-{i}")
        for i in range(12):
            ctx.add_message("A", f"A→B secret {i}", targets=["B"])
        for i in range(10):
            ctx.add_message("系统", f"tail-{i}")

        await ctx.compress_all()

        # {A, B} share one call (identical views incl. the private segment);
        # C's view hashes differently → its own call.
        assert len(provider.calls) == 2, (
            f"expected one call for {{A,B}} and one for C, got {len(provider.calls)}"
        )
        ab_sum = _summaries(ctx.view_for("A"))
        assert len(ab_sum) == 1
        assert _summaries(ctx.view_for("B")) == ab_sum, "A and B share the summary"
        c_sum = _summaries(ctx.view_for("C"))
        assert len(c_sum) == 1
        assert c_sum != ab_sum, "C's summary covers different (public-only) content"
        c_view_text = "\n".join(m["content"] for m in ctx.view_for("C"))
        assert "A→B secret" not in c_view_text, "private content must never reach C"
        assert ab_sum[0] not in c_view_text, "the A/B group summary must never reach C"

    async def test_single_message_difference_is_a_different_group(self, tmp_path):
        """差一条消息即视为不同组: A's view has exactly one extra middle
        message → two distinct calls, no sharing."""
        provider = _HashProvider(content="[压缩摘要 digest]")
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A", "B"])
        for i in range(40):
            ctx.add_message("用户" if i == 0 else "系统", f"public-{i}")
        ctx.add_message("系统", "only-for-A", targets=["A"])
        for i in range(10):
            ctx.add_message("系统", f"tail-{i}")

        await ctx.compress_all()

        assert len(provider.calls) == 2, "one extra middle message must break the group"
        assert len(_summaries(ctx.view_for("A"))) == 1
        assert len(_summaries(ctx.view_for("B"))) == 1
        assert _summaries(ctx.view_for("A")) != _summaries(ctx.view_for("B"))


class TestDedupScope:
    async def test_standalone_compress_for_never_shares(self, tmp_path):
        """Dedup lives inside ONE compress_all batch: sequential compress_for
        calls on identical views each do their own provider call."""
        provider = _HashProvider(content="[压缩摘要 digest]")
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A", "B"])
        for i in range(60):
            ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")

        await ctx.compress_for("A")
        await ctx.compress_for("B")

        assert len(provider.calls) == 2, "compress_for must keep its standalone behaviour"


class TestFailureIsNotShared:
    async def test_provider_failure_keeps_middle_in_every_view(self, tmp_path, compressed_events):
        """A failed summary yields nothing to share: every view keeps its
        middle region uncompressed (no event), matching compress_for."""
        provider = _HashProvider(content=None)  # always raises
        ctx = _make_context(tmp_path, provider=provider)
        ctx.set_active_agents(["A", "B"])
        for i in range(60):
            ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")
        before_a = len(ctx.view_for("A"))

        await ctx.compress_all()

        assert compressed_events == []
        assert len(ctx.view_for("A")) == before_a, "failure must not drop the middle"
        # Each view exhausted its own retry budget (2 attempts × 2 views).
        assert len(provider.calls) == 4
