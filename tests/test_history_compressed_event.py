"""C0.3 (plan.md 批次 C0 第 3 条): history:compressed event.

Compression was invisible to the event bus / mods — no way to observe when a
view got compressed, by how much, or what the summary call cost.  These tests
pin the new ``history:compressed`` event: registered in the EVENTS catalogue,
emitted by HistoryContext on every successful per-view compression, silent
when compression did not happen (disabled / no provider / below threshold /
summary failed).
"""

from __future__ import annotations

import pytest

from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.runtime.events import (
    EVENTS,
    BroadcastEventDispatcher,
    get_bus,
    set_bus,
)


_ABSENT = object()  # distinguish "attribute not set" from explicit None


class _FakeProvider:
    """Records calls; returns a summary response with usage/cost fields."""

    def __init__(self, content: str = "[压缩摘要] fake summary",
                 usage=_ABSENT, cost=_ABSENT) -> None:
        self.calls: list[dict] = []
        self._content = content
        self._usage = {"prompt_tokens": 100, "completion_tokens": 20,
                       "total_tokens": 120} if usage is _ABSENT else usage
        self._cost = 0.004 if cost is _ABSENT else cost

    async def chat_with_retry(self, messages, model=None, max_tokens=None,
                              metadata=None, **kwargs):
        self.calls.append({"messages": messages, "model": model,
                           "max_tokens": max_tokens, "metadata": metadata})
        if self._content is None:
            raise RuntimeError("provider down")

        class _R:
            pass

        r = _R()
        r.content = self._content
        r.finish_reason = "stop"
        if self._usage is not None:
            r.usage = self._usage
        if self._cost is not None:
            r.cost = self._cost
        return r


def _make_context(tmp_path, provider=None) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=provider)


def _fill_history(ctx: HistoryContext, n: int = 60) -> None:
    for i in range(n):
        ctx.add_message("用户" if i % 2 == 0 else "系统", f"msg{i}")


@pytest.fixture(autouse=True)
def _small_compression_window(monkeypatch):
    """Keep algorithm tests small while production defaults retain 200 turns."""
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
    get_bus().on("history:compressed", lambda **kw: events.append(kw) or _noop())
    return events


async def _noop() -> None:
    pass


class TestEventCatalogue:
    def test_history_compressed_is_registered(self):
        """Mods subscribe by catalogue name — the event must be in EVENTS."""
        assert "history:compressed" in EVENTS


class TestEmitOnSuccess:
    async def test_successful_compress_emits_full_payload(self, tmp_path, compressed_events):
        provider = _FakeProvider()
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx)
        view_before = len(ctx.view_for("A"))

        await ctx.compress_for("A")

        view_after = len(ctx.view_for("A"))
        assert len(compressed_events) == 1
        payload = compressed_events[0]
        assert payload["agent"] == "A"
        assert payload["dropped"] > 0
        assert payload["view_before"] == view_before
        assert payload["view_after"] == view_after
        assert payload["view_after"] < payload["view_before"]
        assert payload["model"] == "openai/test-summarizer"
        # tokens/cost come off the summary response object (LLMResponse fields)
        assert payload["prompt_tokens"] == 100
        assert payload["completion_tokens"] == 20
        assert payload["cost"] == 0.004
        assert payload["triggered_by"] == "round_end"

    async def test_tokens_and_cost_are_none_safe(self, tmp_path, compressed_events):
        """A response object without usage/cost (e.g. minimal fake) must yield
        None fields, not raise."""
        provider = _FakeProvider(usage=None, cost=None)  # response without usage/cost attrs
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx)

        await ctx.compress_for("A")

        assert len(compressed_events) == 1
        assert compressed_events[0]["prompt_tokens"] is None
        assert compressed_events[0]["completion_tokens"] is None
        assert compressed_events[0]["cost"] is None

    async def test_triggered_by_direct_reply_forwarded(self, tmp_path, compressed_events):
        ctx = _make_context(tmp_path, provider=_FakeProvider())
        _fill_history(ctx)

        await ctx.compress_for("A", triggered_by="direct_reply")

        assert compressed_events[0]["triggered_by"] == "direct_reply"

    async def test_compress_all_forwards_triggered_by_per_agent(self, tmp_path, compressed_events):
        ctx = _make_context(tmp_path, provider=_FakeProvider())
        _fill_history(ctx)
        ctx.set_active_agents(["A", "B"])

        await ctx.compress_all(triggered_by="direct_reply")

        assert {e["agent"] for e in compressed_events} == {"A", "B"}
        assert all(e["triggered_by"] == "direct_reply" for e in compressed_events)


class TestNoEmitWhenCompressionDidNotHappen:
    async def test_no_emit_when_summarization_disabled(self, tmp_path, compressed_events):
        """No provider = summarisation disabled: no event, middle kept."""
        ctx = _make_context(tmp_path, provider=None)
        _fill_history(ctx, n=70)

        await ctx.compress_for("A")

        assert compressed_events == []

    async def test_no_emit_below_threshold(self, tmp_path, compressed_events):
        provider = _FakeProvider()
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx, n=10)  # below 50*0.8

        await ctx.compress_for("A")

        assert compressed_events == []
        assert provider.calls == []

    @pytest.mark.parametrize("failure", ["empty_summary", "provider_raises"])
    async def test_no_emit_when_summary_fails(self, tmp_path, compressed_events, failure):
        """Provider down / empty summary: middle is kept, no event."""
        provider = _FakeProvider(content="" if failure == "empty_summary" else None)
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx)

        await ctx.compress_for("A")

        assert compressed_events == []


class TestEngineSeam:
    async def test_maybe_compress_history_forwards_triggered_by(self, tmp_path, compressed_events):
        """engine._maybe_compress_history(triggered_by=...) is the seam the
        direct-chat call site uses; it must forward into compress_all."""
        from nanobot.groupchat.runtime.engine import GroupChatEngine

        ctx = _make_context(tmp_path, provider=_FakeProvider())
        _fill_history(ctx)

        class _EngineStub:
            # Only the attributes _maybe_compress_history touches
            history = ctx
            _active_agents = ["A"]

        await GroupChatEngine._maybe_compress_history(
            _EngineStub(), triggered_by="direct_reply"
        )

        assert [e["agent"] for e in compressed_events] == ["A"]
        assert compressed_events[0]["triggered_by"] == "direct_reply"

    async def test_maybe_compress_history_defaults_to_round_end(self, tmp_path, compressed_events):
        """The round-end call site (run_loop) relies on the default."""
        from nanobot.groupchat.runtime.engine import GroupChatEngine

        ctx = _make_context(tmp_path, provider=_FakeProvider())
        _fill_history(ctx)

        class _EngineStub:
            history = ctx
            _active_agents = ["A"]

        await GroupChatEngine._maybe_compress_history(_EngineStub())

        assert compressed_events[0]["triggered_by"] == "round_end"
