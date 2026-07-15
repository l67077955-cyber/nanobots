"""Tests for session log discovery and recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.cli.logs import recover_conversation, resolve_session_dir
from nanobot.groupchat.context.persistence import GroupChatState


@pytest.fixture()
def nanobot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import nanobot.cli.logs as logs_mod
    import nanobot.groupchat.context.persistence as persistence_mod

    monkeypatch.setattr(persistence_mod, "_NANOBOT_DIR", tmp_path)
    monkeypatch.setattr(logs_mod, "_NANOBOT_DIR", tmp_path)
    monkeypatch.setattr(logs_mod, "_SESSIONS_DIR", tmp_path / "collab-sessions")
    return tmp_path


def test_save_current_session_pointer(nanobot_home: Path) -> None:
    state = GroupChatState(registry={})
    session_dir = nanobot_home / "collab-sessions" / "gc-20260620-141846"
    session_dir.mkdir(parents=True)
    state.save_current_session(
        session_dir,
        topic="自由讨论",
        round_num=2,
        agents=["Harper", "Kirk"],
        leader="Harper",
    )
    loaded = state.load_current_session()
    assert loaded is not None
    assert loaded["session_id"] == "gc-20260620-141846"
    assert loaded["topic"] == "自由讨论"
    assert loaded["leader"] == "Harper"


def test_resolve_session_from_current_pointer(nanobot_home: Path) -> None:
    session_dir = nanobot_home / "collab-sessions" / "gc-test"
    session_dir.mkdir(parents=True)
    (nanobot_home / "current_session.json").write_text(
        json.dumps({"session_id": "gc-test", "session_dir": str(session_dir)}),
        encoding="utf-8",
    )
    assert resolve_session_dir() == session_dir
    assert resolve_session_dir("gc-test") == session_dir


def test_recover_conversation_from_session_jsonl(nanobot_home: Path) -> None:
    session_dir = nanobot_home / "collab-sessions" / "gc-test"
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text(
        json.dumps(
            {"type": "message", "ts": "t1", "agent": "用户", "content": "做页面"},
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {"type": "message", "ts": "t2", "agent": "Harper", "content": "收到"},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    text = recover_conversation(session_dir)
    assert "做页面" in text
    assert "收到" in text


def test_logs_cli_show(nanobot_home: Path) -> None:
    session_dir = nanobot_home / "collab-sessions" / "gc-cli"
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "session_start",
                "ts": "2026-06-20T14:18:46+08:00",
                "topic": "demo",
                "mode": "broadcast",
                "leader": "Harper",
                "agents": ["Harper"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (nanobot_home / "current_session.json").write_text(
        json.dumps({"session_id": "gc-cli", "session_dir": str(session_dir)}),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 0
    assert "gc-cli" in result.stdout


