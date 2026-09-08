"""C0.2 (plan.md 批次 C0 第 2 条): compression LLM calls carry metadata.

The summarisation calls fired by history compression used to be bare
``chat_with_retry(messages=..., model=..., max_tokens=...)`` calls, so their
request_logs entries had ``agent=null, mode=null`` — compression spend could
only be guessed at.  These tests pin that both compression paths now identify
themselves through the repo's established metadata convention:
``metadata["log_agent"] / metadata["log_mode"]`` are the keys
``litellm_provider._log_request`` maps onto the request_logs record's
``agent`` / ``mode`` fields (verified against the real mapping in the last
test class, not assumed).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary


class _CapturingProvider:
    """Minimal fake provider: records chat_with_retry kwargs, returns a summary."""

    def __init__(self, content: str = "[压缩摘要] fake summary") -> None:
        self.calls: list[dict] = []
        self._content = content

    async def chat_with_retry(self, messages, model=None, max_tokens=None,
                              metadata=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
            "metadata": metadata,
        })

        class _R:
            pass

        r = _R()
        r.content = self._content
        r.finish_reason = "stop"
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


class TestHistoryCompressMetadata:
    async def test_compress_for_summary_call_is_attributed(self, tmp_path):
        """compress_for("A") → the summary request carries
        log_mode="history_compress" and log_agent="A" so its request_logs
        entry is attributable instead of agent=null/mode=null."""
        provider = _CapturingProvider()
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx)

        await ctx.compress_for("A")

        assert len(provider.calls) == 1
        meta = provider.calls[0]["metadata"]
        assert meta is not None, "summary call must pass metadata"
        assert meta["log_mode"] == "history_compress"
        assert meta["log_agent"] == "A"
        # The call itself is otherwise unchanged
        assert provider.calls[0]["model"] == "openai/test-summarizer"
        assert provider.calls[0]["max_tokens"] == 600

    async def test_compress_all_attributes_each_call_to_its_agent(self, tmp_path):
        """compress_all → one attributed call per content GROUP (C1.2 dedup):
        identical views share the first group member's call; views with
        different middle content each get their own attributed call."""
        provider = _CapturingProvider()
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx)
        ctx.set_active_agents(["A", "B"])

        await ctx.compress_all()

        # A and B saw identical messages → one shared call, attributed to the
        # first view of the group (request_logs still says history_compress).
        assert len(provider.calls) == 1
        assert provider.calls[0]["metadata"]["log_agent"] == "A"
        assert provider.calls[0]["metadata"]["log_mode"] == "history_compress"

        # Differing middle content → separate attributed calls per view.
        provider2 = _CapturingProvider()
        ctx2 = _make_context(tmp_path / "b", provider=provider2)
        _fill_history(ctx2)
        ctx2.add_message("系统", "private for B", targets=["B"])
        for i in range(10):
            ctx2.add_message("系统", f"tail-{i}")
        ctx2.set_active_agents(["A", "B"])

        await ctx2.compress_all()

        assert {c["metadata"]["log_agent"] for c in provider2.calls} == {"A", "B"}
        assert all(c["metadata"]["log_mode"] == "history_compress" for c in provider2.calls)


class TestTailSummarizeMetadata:
    def _make_long_conversation(self, n_pairs: int) -> list[dict]:
        msgs = [{"role": "system", "content": "system"}]
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": f"user msg {i}"})
            msgs.append({"role": "assistant", "content": f"assistant msg {i}"})
        return msgs

    async def test_tail_summary_call_is_attributed(self):
        """prune_conversation_tail_with_summary's LLM call carries
        log_mode="tail_summarize" and the agent's name — distinguishable
        from HistoryContext's history_compress calls in request_logs."""
        provider = _CapturingProvider(content="## 关键进展\nfake")
        msgs = self._make_long_conversation(20)  # 40 conv msgs, drop 34

        dropped = await prune_conversation_tail_with_summary(
            msgs, 1, keep_turns=3,
            provider=provider, model="openai/test-summarizer",
            agent_name="Alpha", min_dropped_for_summary=5,
        )

        assert dropped > 10
        assert len(provider.calls) == 1
        meta = provider.calls[0]["metadata"]
        assert meta is not None, "tail summary call must pass metadata"
        assert meta["log_mode"] == "tail_summarize"
        assert meta["log_agent"] == "Alpha"
        assert provider.calls[0]["model"] == "openai/test-summarizer"


class TestMetadataMapsToRequestLogFields:
    def test_log_keys_map_to_agent_and_mode_fields(self, tmp_path, monkeypatch):
        """Verify against the real provider logging path (not an assumption):
        metadata["log_agent"]/["log_mode"] land in the request_logs record's
        ``agent``/``mode`` fields — this is the attribution C0.2 exists for."""
        from nanobot.providers.litellm_provider import LiteLLMProvider

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        LiteLLMProvider._log_request(
            {
                "metadata": {"log_agent": "A", "log_mode": "history_compress"},
                "model": "openai/test-summarizer",
                "messages": [{"role": "user", "content": "summarize this"}],
            },
            response=None,
        )
        log_file = (
            tmp_path / ".nanobot" / "request_logs" / f"{time.strftime('%Y-%m-%d')}.jsonl"
        )
        record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
        assert record["agent"] == "A"
        assert record["mode"] == "history_compress"
