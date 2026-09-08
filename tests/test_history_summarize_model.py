"""C1.3 (plan.md 批次 C1 第 3 条 / W9): dedicated history summary model.

History compression reused ``tool_results.summarize_model`` — one knob
steered two unrelated workloads (tool-result summarisation inside agent
turns vs whole-view compression at round end), so tuning one silently
retuned the other.  These tests pin the new ``history.summarize_model``
setting and its resolution:

  * default ``None`` → falls back to ``tool_results.summarize_model``
    (backward compatible: deployments without the key behave identically);
  * ``~/.nanobot/history_settings.json`` override wins when set (and an
    empty string counts as unset);
  * HistoryContext's compression call (and its ``history:compressed``
    event) resolve through the new getter.
"""

from __future__ import annotations

import json

import pytest

from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.runtime.events import BroadcastEventDispatcher, get_bus, set_bus


class _ModelCapturingProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_with_retry(self, messages, model=None, max_tokens=None, **kw):
        self.calls.append({"model": model})

        class _R:
            content = "[压缩摘要] fake"
            finish_reason = "stop"

        return _R()


def _make_context(tmp_path, provider=None) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=provider)


def _fill_history(ctx: HistoryContext, n: int = 60) -> None:
    for i in range(n):
        ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")


@pytest.fixture(autouse=True)
def _small_compression_window(monkeypatch):
    monkeypatch.setattr(history_settings, "max_messages", lambda: 50)
    monkeypatch.setattr(history_settings, "max_context_chars", lambda: 0)
    monkeypatch.setattr(history_settings, "compress_ratio", lambda: 0.8)
    monkeypatch.setattr(history_settings, "compression_keep_recent", lambda: 6)
    monkeypatch.setattr(history_settings, "keep_user_messages", lambda: False)
    monkeypatch.setattr(history_settings, "history_summarize_enabled", lambda: True)
    monkeypatch.setattr(history_settings, "compress_max_summary_tokens", lambda: 600)


@pytest.fixture(autouse=True)
def _isolated_bus():
    set_bus(BroadcastEventDispatcher())
    yield
    set_bus(BroadcastEventDispatcher())


@pytest.fixture()
def _local_settings_file(tmp_path, monkeypatch):
    """Point the settings singleton at a fresh ~/.nanobot/history_settings.json."""
    path = tmp_path / "history_settings.json"
    monkeypatch.setattr(history_settings, "_SETTINGS_FILE", path)
    monkeypatch.setattr(history_settings, "_cache", None)
    return path


class TestSettingsResolution:
    def test_default_is_none_and_falls_back_to_tool_results(self, _local_settings_file):
        """No local file → history.summarize_model is None (unset) and the
        resolver returns tool_results.summarize_model verbatim."""
        assert history_settings.get_all()["history"]["summarize_model"] is None
        assert (
            history_settings.history_summarize_model()
            == history_settings.summarize_model()
            == "openai/gpt-4.1-nano"
        )

    def test_local_file_without_key_still_falls_back(self, _local_settings_file):
        """A pre-C1.3 settings file (key absent) must behave exactly as
        before — the local override mechanism must not invent a value."""
        _local_settings_file.write_text(
            json.dumps({"history": {"max_messages": 123}}), encoding="utf-8"
        )
        history_settings.reload()

        # The file merged on top of defaults (get_all, since the autouse
        # window fixture patches the max_messages getter for other tests).
        assert history_settings.get_all()["history"]["max_messages"] == 123
        assert (
            history_settings.history_summarize_model()
            == history_settings.summarize_model()
        )

    def test_local_override_wins(self, _local_settings_file):
        _local_settings_file.write_text(
            json.dumps({"history": {"summarize_model": "openai/history-only"}}),
            encoding="utf-8",
        )
        history_settings.reload()

        assert history_settings.history_summarize_model() == "openai/history-only"
        # The two knobs are now independent: tool_results is untouched.
        assert history_settings.summarize_model() == "openai/gpt-4.1-nano"

    def test_empty_string_counts_as_unset(self, _local_settings_file):
        _local_settings_file.write_text(
            json.dumps({"history": {"summarize_model": ""}}), encoding="utf-8"
        )
        history_settings.reload()

        assert (
            history_settings.history_summarize_model()
            == history_settings.summarize_model()
        )


class TestContextUsesHistoryModel:
    async def test_unset_history_model_falls_back_at_the_call_site(
        self, tmp_path, monkeypatch
    ):
        """With history.summarize_model unresolved (the getter falls back),
        the compression call uses tool_results.summarize_model — the
        pre-C1.3 behaviour existing deployments rely on."""
        monkeypatch.setattr(history_settings, "summarize_model", lambda: "openai/tools-m")
        monkeypatch.setattr(
            history_settings, "history_summarize_model", lambda: "openai/tools-m"
        )
        provider = _ModelCapturingProvider()
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx)

        await ctx.compress_for("A")

        assert [c["model"] for c in provider.calls] == ["openai/tools-m"]

    async def test_history_model_override_reaches_provider_and_event(
        self, tmp_path, monkeypatch, _local_settings_file
    ):
        """Setting history.summarize_model must steer ONLY the compression
        call: the provider sees it and the history:compressed event reports
        it, while tool_results.summarize_model is left alone."""
        events: list[dict] = []

        async def _rec(**kw):
            events.append(kw)

        get_bus().on("history:compressed", _rec)
        _local_settings_file.write_text(
            json.dumps({"history": {"summarize_model": "openai/history-m"}}),
            encoding="utf-8",
        )
        history_settings.reload()

        provider = _ModelCapturingProvider()
        ctx = _make_context(tmp_path, provider=provider)
        _fill_history(ctx)

        await ctx.compress_for("A")

        assert [c["model"] for c in provider.calls] == ["openai/history-m"]
        assert len(events) == 1
        assert events[0]["model"] == "openai/history-m"
        # tool_results knob unaffected by the history override
        assert history_settings.summarize_model() == "openai/gpt-4.1-nano"
