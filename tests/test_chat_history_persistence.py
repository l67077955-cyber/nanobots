"""Tests for chat history persistence across gateway restarts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.orchestra.engine import GroupChatEngine


@pytest.fixture()
def history_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import nanobot.groupchat.history.persistence as persistence_mod

    target = tmp_path / "chat_history.json"
    monkeypatch.setattr(persistence_mod, "_NANOBOT_DIR", tmp_path)
    return target


def test_save_and_load_history_snapshot(history_file: Path) -> None:
    state = GroupChatState(registry={})
    state.save_history_snapshot(
        history=[{"sender": "用户", "content": "做 HTML 页面"}],
        topic="自由讨论",
        round_num=1,
        session_dir=Path("/tmp/gc-test"),
    )
    loaded = state.load_history_snapshot()
    assert loaded is not None
    assert loaded["history"][0]["content"] == "做 HTML 页面"
    assert loaded["topic"] == "自由讨论"
    assert loaded["round"] == 1
    assert loaded["session_dir"] == "/tmp/gc-test"


def test_clear_history_snapshot(history_file: Path) -> None:
    state = GroupChatState(registry={})
    state.save_history_snapshot(
        history=[{"sender": "用户", "content": "test"}],
    )
    state.clear_history_snapshot()
    assert state.load_history_snapshot() is None


def test_engine_restores_history_on_init(history_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.groupchat.history.persistence as persistence_mod

    monkeypatch.setattr(persistence_mod, "_NANOBOT_DIR", history_file.parent)
    history_file.write_text(
        json.dumps(
            {
                "history": [
                    {"sender": "用户", "content": "2077 风格 Claude 介绍页"},
                    {"sender": "Kirk", "content": "直接写HTML"},
                ],
                "topic": "自由讨论",
                "round": 2,
                "session_dir": "",
                "updated_at": "2026-06-20T14:25:00+08:00",
            },
            ensure_ascii=False,
        )
    )

    provider = MagicMock()
    engine = GroupChatEngine(GroupChatConfig(), provider, Path("/tmp"))
    engine.registry = {"Harper": {"model": "test/model"}, "Kirk": {"model": "test/model"}}

    history = engine.history.to_sender_dicts()
    assert len(history) == 2
    assert history[0]["content"] == "2077 风格 Claude 介绍页"
    assert engine._topic == "自由讨论"
    assert engine._round == 2


def test_clear_history_removes_snapshot(history_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.groupchat.history.persistence as persistence_mod

    monkeypatch.setattr(persistence_mod, "_NANOBOT_DIR", history_file.parent)
    provider = MagicMock()
    engine = GroupChatEngine(GroupChatConfig(), provider, Path("/tmp"))
    engine._add_message("用户", "round 1 task")
    assert history_file.exists()

    engine.clear_history()
    assert not history_file.exists()
    assert len(engine.history) == 0


def test_engine_restore_preserves_compact_boundary_flag(
    history_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The is_compact_summary flag must survive a gateway restart.

    Regression for the restore filter at GroupChatEngine._restore_chat_state,
    which previously rebuilt each message as {sender, content} only and
    silently dropped the structured compact-boundary flag — forcing the
    legacy string-prefix fallback to do all the work.
    """
    import nanobot.groupchat.history.persistence as persistence_mod

    monkeypatch.setattr(persistence_mod, "_NANOBOT_DIR", history_file.parent)
    history_file.write_text(
        json.dumps(
            {
                "history": [
                    {"sender": "用户", "content": "before compaction"},
                    {
                        "sender": "系统",
                        "content": "[早期对话摘要（压缩了 3 条中间消息）]\n摘要正文",
                        "is_compact_summary": True,
                    },
                    {"sender": "AgentA", "content": "after compaction"},
                ],
                "topic": "",
                "round": 0,
                "session_dir": "",
                "updated_at": "2026-07-07T10:00:00+08:00",
            },
            ensure_ascii=False,
        )
    )

    provider = MagicMock()
    engine = GroupChatEngine(GroupChatConfig(), provider, Path("/tmp"))

    restored = engine.history.to_sender_dicts()
    assert len(restored) == 3
    summary_msg = next(m for m in restored if m["sender"] == "系统")
    assert summary_msg.get("is_compact_summary") is True, (
        "is_compact_summary flag was stripped on restore"
    )
    # Non-flag messages stay clean (no spurious keys)
    assert "is_compact_summary" not in restored[0]
    assert "is_compact_summary" not in restored[2]
