"""Regression tests for the safer default group-chat history window."""

from __future__ import annotations

from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState


def test_default_history_window_keeps_two_hundred_messages(monkeypatch, tmp_path):
    """Default settings must not trim a normal long group-chat discussion."""
    monkeypatch.setattr(history_settings, "_SETTINGS_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(history_settings, "_cache", None)

    assert history_settings.max_messages() == 200
    assert history_settings.compression_keep_recent() == 20

    ctx = HistoryContext(GroupChatState({"A": {}}, state_dir=tmp_path), provider=None)
    for index in range(200):
        ctx.add_message("A", f"message-{index}")

    assert len(ctx.all_messages()) == 200
    assert ctx.all_messages()[0]["content"] == "message-0"
