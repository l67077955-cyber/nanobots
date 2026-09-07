"""Regression tests for HistoryContext's public Phase E contract."""

from __future__ import annotations

from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState


def _make_context(tmp_path) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=None)


def test_query_methods_do_not_expose_or_require_the_internal_log(tmp_path):
    ctx = _make_context(tmp_path)

    assert ctx.is_empty()
    assert ctx.last_sender() is None
    assert not ctx.has_system_message()

    ctx.add_message("系统", "topic")
    ctx.add_message("A", "private", targets=["B"])

    assert not ctx.is_empty()
    assert ctx.has_system_message()
    assert ctx.last_sender() == "A"

    log_copy = ctx.all_messages()
    log_copy[-1]["content"] = "tampered"
    log_copy.clear()
    assert [m["content"] for m in ctx.all_messages()] == ["topic", "private"]


def test_view_for_raw_is_visibility_filtered_but_not_a_mutable_view(tmp_path):
    ctx = _make_context(tmp_path)
    ctx.add_message("A", "for B", targets=["B"])

    raw = ctx.view_for_raw("B")
    raw[0]["content"] = "tampered"

    assert [m["content"] for m in ctx.view_for_raw("B")] == ["for B"]
    assert ctx.view_for_raw("C") == []


def test_clear_agent_view_only_resets_that_agents_persistent_context(tmp_path):
    ctx = _make_context(tmp_path)
    ctx._active_agents = ["A", "B"]
    ctx.add_message("用户", "question")
    ctx.add_message("A", "old A answer")
    ctx.add_message("A", "latest A answer")
    ctx.add_message("B", "B answer")

    assert ctx.clear_agent_view("A", keep_last=1) == 1
    assert [m["content"] for m in ctx.view_for("A")] == [
        "question", "latest A answer", "B answer"
    ]
    assert [m["content"] for m in ctx.view_for("B")] == [
        "question", "old A answer", "latest A answer", "B answer"
    ]
