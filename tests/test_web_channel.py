"""Tests for the web chat channel."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nanobot.channels.web import WebChannel, WebConfig  # noqa: E402


def test_web_config_defaults() -> None:
    cfg = WebConfig()
    assert cfg.port == 18791
    assert "*" in cfg.allow_from
    assert cfg.serve_ws is True


def test_web_channel_name() -> None:
    ch = WebChannel(WebConfig(), bus=None)  # type: ignore[arg-type]
    assert ch.name == "web"