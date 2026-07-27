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
def workspace(tmp_path: Path) -> Path:
    """Create a temp workspace with .groupchat state dir."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".groupchat").mkdir()
    return workspace


def test_save_and_load_history_snapshot(workspace: Path) -> None:
    state = GroupChatState(registry={}, workspace=workspace)
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


def test_clear_history_snapshot(workspace: Path) -> None:
    state = GroupChatState(registry={}, workspace=workspace)
    state.save_history_snapshot(
        history=[{"sender": "用户", "content": "test"}],
    )
    state.clear_history_snapshot()
    assert state.load_history_snapshot() is None


def test_engine_restores_history_on_init(workspace: Path) -> None:
    history_file = workspace / ".groupchat" / "chat_history.json"
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
    engine = GroupChatEngine(GroupChatConfig(), provider, workspace)
    engine.registry = {"Harper": {"model": "test/model"}, "Kirk": {"model": "test/model"}}

    assert len(engine._history) == 2
    assert engine._history[0]["content"] == "2077 风格 Claude 介绍页"
    assert engine._topic == "自由讨论"
    assert engine._round == 2


def test_clear_history_removes_snapshot(workspace: Path) -> None:
    history_file = workspace / ".groupchat" / "chat_history.json"
    provider = MagicMock()
    engine = GroupChatEngine(GroupChatConfig(), provider, workspace)
    engine.registry = {"Harper": {"model": "test/model"}}
    engine._add_message("用户", "round 1 task")
    assert history_file.exists()

    engine.clear_history()
    assert not history_file.exists()
    assert engine._history == []