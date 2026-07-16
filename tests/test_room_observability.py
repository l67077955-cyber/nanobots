"""Tests for phase-0 room observability (no Telegram behavior change)."""

from __future__ import annotations

import json
from pathlib import Path

import nanobot.groupchat.runtime.room_observability as obs


def test_emit_and_ring_buffer(tmp_path, monkeypatch):
    log_file = tmp_path / "room_events.jsonl"
    monkeypatch.setattr(obs, "_LOG_PATH", log_file)
    monkeypatch.setattr(obs, "_ring", {})
    monkeypatch.setenv("NANOBOT_ROOM_OBS", "1")

    obs.emit_room_event(
        room_id="telegram:123",
        kind="user_input",
        agent="User",
        content="hello",
    )
    obs.emit_room_event(
        room_id="telegram:123",
        kind="ui_push",
        content="── User ──\nhello",
        extra={"chars": 10},
    )

    recent = obs.get_recent_events("telegram:123")
    assert len(recent) == 2
    assert recent[0]["kind"] == "user_input"
    assert recent[1]["kind"] == "ui_push"

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["room_id"] == "telegram:123"


def test_disabled_via_env(monkeypatch):
    monkeypatch.setattr(obs, "_ring", {})
    monkeypatch.setenv("NANOBOT_ROOM_OBS", "0")

    obs.emit_room_event(room_id="default", kind="user_input", content="x")
    assert obs.get_recent_events("default") == []


def test_resolve_room_id():
    assert obs.resolve_room_id("telegram", "8008274300") == "telegram:8008274300"
    assert obs.resolve_room_id(None, None) == "default"