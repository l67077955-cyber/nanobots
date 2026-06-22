"""Tests for /history panel builder."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def sample_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = {
        "context_window_tokens": 35000,
        "tool_result_max_chars": 5000,
        "tool_results": {
            "exec_max_chars": 3500,
            "web_fetch_max_chars": 5000,
            "web_search_max_chars": 5000,
            "summarize_enabled": True,
            "summarize_threshold": 20000,
            "summarize_model": "openai/gpt-4.1-nano",
            "broadcast_result_max_chars": 25000,
            "direct_result_max_chars": 12000,
            "read_file_max_chars": 5000,
        },
        "history": {
            "max_messages": 30,
            "max_context_chars": 30000,
            "compress_ratio": 0.7,
            "compress_max_summary_tokens": 2000,
            "compression_keep_recent": 8,
            "keep_user_messages": True,
            "history_summarize_enabled": True,
        },
        "context_pruning": {
            "soft_ratio": 0.1,
            "keep_recent": 10,
            "soft_max_chars": 3000,
        },
    }
    path = tmp_path / "history_settings.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(
        "nanobot.groupchat.history.history_settings._SETTINGS_FILE",
        path,
    )
    from nanobot.groupchat.history import history_settings as hs

    hs.reload()
    return cfg


def test_config_warnings_detect_threshold_mismatch(sample_settings):
    from nanobot.channels.telegram.history_panel import collect_config_warnings

    warnings = collect_config_warnings()
    joined = "\n".join(warnings)
    assert "summarize_threshold" in joined
    assert "未接入" in joined
    assert "read_file_max_chars" in joined


def test_main_panel_mentions_algorithm_aligned_terms(sample_settings):
    from nanobot.channels.telegram.history_panel import build_main_panel_text

    text = build_main_panel_text(engine=None)
    assert "head_only" in text
    assert "maybe_compress" in text
    assert "prune_messages" in text
    assert "compression_keep_recent" not in text  # uses rendered value "尾保 8条"
    assert "尾保 8条" in text
    assert "全部用户消息" in text


def test_expanded_panel_includes_demo(sample_settings):
    from nanobot.channels.telegram.history_panel import build_main_panel_text

    compact = build_main_panel_text(engine=None, expanded=False)
    expanded = build_main_panel_text(engine=None, expanded=True)
    assert "管线详解" not in compact
    assert "管线详解" in expanded
    assert len(expanded) > len(compact)


def test_live_metrics_with_mock_engine(sample_settings):
    from nanobot.channels.telegram.history_panel import collect_live_metrics

    class _History:
        _compress_warned = True

    class _Engine:
        _history = [
            {"sender": "User", "content": "hello"},
            {"sender": "ponytail", "content": "world" * 100},
        ]
        _active_agents = []
        history = _History()

    metrics = collect_live_metrics(_Engine())
    assert metrics["current_msgs"] == 2
    assert metrics["compress_warned"] is True
    assert metrics["tok_pct"] >= 0