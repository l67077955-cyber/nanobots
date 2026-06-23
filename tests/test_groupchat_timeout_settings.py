"""Tests for configurable groupchat timeout settings."""

from __future__ import annotations

import json
from pathlib import Path

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