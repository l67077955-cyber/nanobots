"""Tests for configurable groupchat timeout settings."""

from __future__ import annotations

import json
from pathlib import Path

from nanobot.groupchat.orchestra.broadcast_agent import (
    _resolve_call_timeout,
    _resolve_loop_limits,
)
from nanobot.groupchat.orchestra.broadcast_orchestrator import load_groupchat_settings


def test_load_groupchat_settings_defaults(tmp_path: Path, monkeypatch) -> None:
    import nanobot.groupchat.orchestra.broadcast_orchestrator as mod

    monkeypatch.setattr(mod.Path, "home", lambda: tmp_path)
    settings = load_groupchat_settings()
    assert settings["call_timeout"] == 90
    assert settings["leader_call_timeout"] == 120
    assert settings["global_timeout"] == 600


def test_load_groupchat_settings_from_file(tmp_path: Path, monkeypatch) -> None:
    import nanobot.groupchat.orchestra.broadcast_orchestrator as mod

    monkeypatch.setattr(mod.Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".nanobot"
    cfg.mkdir(parents=True)
    (cfg / "groupchat_settings.json").write_text(
        json.dumps({"call_timeout": 180, "global_timeout": 1800})
    )
    settings = load_groupchat_settings()
    assert settings["call_timeout"] == 180
    assert settings["global_timeout"] == 1800
    assert settings["leader_call_timeout"] == 120


def test_single_agent_broadcast_limits_are_capped() -> None:
    assert _resolve_loop_limits(is_leader=True, total=1) == (4, 3)
    assert _resolve_loop_limits(is_leader=False, total=1) == (4, 3)
    assert _resolve_call_timeout(
        {"leader_call_timeout": 240, "call_timeout": 180},
        is_leader=True,
        total=1,
    ) == 75.0


def test_multi_agent_broadcast_limits_keep_original_budget() -> None:
    assert _resolve_loop_limits(is_leader=True, total=2) == (12, 30)
    assert _resolve_loop_limits(is_leader=False, total=2) == (8, 20)
    assert _resolve_call_timeout(
        {"leader_call_timeout": 240, "call_timeout": 180},
        is_leader=True,
        total=2,
    ) == 240.0
