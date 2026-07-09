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
            "summarize_max_input_chars": 8000,
            "summarize_max_output_chars": 4000,
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
        "tool_limits": {
            "read_file_max_chars": 5000,
            "read_file_default_lines": 300,
            "list_dir_default_max": 200,
            "exec_max_timeout": 600,
            "exec_max_output": 10000,
        },
        "tool_log_preview": {
            "web_search": 1500,
            "web_fetch": 1500,
            "read_file": 1500,
            "exec": 500,
            "list_dir": 300,
            "chatroom_send": 200,
            "wait": 200,
            "write_file": 300,
            "edit_file": 300,
            "_default": 500,
            "_total_cap": 4000,
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
    yield cfg
    hs._cache = None  # Force reload from real file on next access


def test_config_warnings_detect_threshold_mismatch(sample_settings):
    from nanobot.channels.telegram.history_panel import collect_config_warnings

    warnings = collect_config_warnings()
    joined = "\n".join(warnings)
    assert "压缩阈值" in joined
    assert "工具截断上限" in joined


def test_main_panel_shows_grouped_layout(sample_settings):
    from nanobot.channels.telegram.history_panel import build_main_panel_text

    text = build_main_panel_text(engine=None)
    # New grouped layout terms
    assert "记忆范围" in text
    assert "压缩策略" in text
    assert "工具限制" in text
    assert "跨轮可见性" in text
    assert "全局" in text
    # User-facing values
    assert "尾保8条" in text
    assert "全部用户" in text
    # No raw S1-S6 numbering
    assert "S1" not in text
    assert "S2" not in text


def test_main_panel_shows_status_dashboard(sample_settings):
    from nanobot.channels.telegram.history_panel import build_main_panel_text

    text = build_main_panel_text(engine=None)
    assert "实时状态" in text
    assert "容量" in text
    assert "安全" in text


def test_expanded_panel_includes_flow_demo(sample_settings):
    from nanobot.channels.telegram.history_panel import build_main_panel_text

    compact = build_main_panel_text(engine=None, expanded=False)
    expanded = build_main_panel_text(engine=None, expanded=True)
    assert "管线流程" not in compact
    assert "管线流程" in expanded
    assert "head_only" in expanded
    assert "maybe_compress" in expanded
    assert "prune_messages" in expanded
    assert len(expanded) > len(compact)


def test_group_panel_memory(sample_settings):
    from nanobot.channels.telegram.history_panel import build_group_panel

    text, markup = build_group_panel(None, "memory")
    assert "记忆范围" in text
    assert "最大消息数" in text
    assert "保留最近" in text
    assert "保护用户消息" in text
    # Has edit buttons
    assert any("hs_edit" in str(b.callback_data) for row in markup.inline_keyboard for b in row)
    # Has the unified close button + a back button
    cb_data = [str(b.callback_data) for row in markup.inline_keyboard for b in row]
    assert "close" in cb_data
    assert "hs_back" in cb_data


def test_main_panel_has_close_button(sample_settings):
    from nanobot.channels.telegram.history_panel import build_history_panel

    _, markup = build_history_panel(None)
    cb_data = [str(b.callback_data) for row in markup.inline_keyboard for b in row]
    assert "close" in cb_data


def test_group_panel_compress_advanced(sample_settings):
    from nanobot.channels.telegram.history_panel import build_group_panel

    text_basic, markup_basic = build_group_panel(None, "compress", advanced=False)
    text_adv, markup_adv = build_group_panel(None, "compress", advanced=True)
    assert "高级" not in text_basic
    assert "高级" in text_adv
    assert "广播模式" in text_adv
    assert "广播模式" not in text_basic


def test_group_panel_vis(sample_settings):
    from nanobot.channels.telegram.history_panel import build_group_panel

    text, markup = build_group_panel(None, "vis")
    assert "跨轮可见性" in text
    assert "read预览" in text
    assert "总上限" in text


def test_find_group_for_param(sample_settings):
    from nanobot.channels.telegram.history_panel import find_group_for_param

    assert find_group_for_param("history", "max_messages") == "memory"
    assert find_group_for_param("tool_results", "exec_max_chars") == "compress"
    assert find_group_for_param("tool_limits", "exec_max_output") == "tools"
    assert find_group_for_param("tool_log_preview", "read_file") == "vis"
    assert find_group_for_param("__top__", "context_window_tokens") == "global"


def test_live_metrics_with_mock_engine(sample_settings):
    from nanobot.channels.telegram.history_panel import collect_live_metrics
    from nanobot.core.history import History

    class _Engine:
        history = History.from_sender_dicts([
            {"sender": "User", "content": "hello"},
            {"sender": "ponytail", "content": "world" * 100},
        ])
        _active_agents = []

    metrics = collect_live_metrics(_Engine())
    assert metrics["current_msgs"] == 2
    assert "compress_warned" not in metrics  # dead metric removed
    assert metrics["compress_ready"] is False  # 2/30 msgs, low tokens
    assert metrics["tok_pct"] >= 0


def test_restore_defaults_all(sample_settings):
    from nanobot.channels.telegram.history_panel import restore_defaults

    msg = restore_defaults(None)
    assert "全部" in msg


def test_restore_defaults_group(sample_settings):
    from nanobot.channels.telegram.history_panel import restore_defaults

    msg = restore_defaults("compress")
    assert "压缩策略" in msg


def test_memory_advanced_shows_new_compaction_fields(sample_settings):
    """Phase-1 knobs are exposed in the memory group's advanced panel."""
    from nanobot.channels.telegram.history_panel import build_group_panel

    text, markup = build_group_panel(None, "memory", advanced=True)
    assert "高级" in text
    # The three newly added, previously-hardcoded knobs are now visible
    assert "token触发比" in text
    assert "预算占比" in text
    assert "回退压缩字数" in text
    # ...and editable (edit buttons carry their section:key)
    cb_data = [str(b.callback_data) for row in markup.inline_keyboard for b in row]
    assert any("hs_edit:history:token_trigger_ratio" in cb for cb in cb_data), (
        "token_trigger_ratio missing an edit button"
    )
    assert any("hs_edit:history:context_budget_ratio" in cb for cb in cb_data)
    assert any("hs_edit:history:compress_fallback_chars" in cb for cb in cb_data)


def test_compress_ready_uses_token_trigger_ratio_not_hardcoded(monkeypatch):
    """compress_ready must honour the configurable token_trigger_ratio,
    not the old hardcoded 0.55. With tok_pct=50% and compress_ratio=0.99:
      token_trigger_ratio=0.6 -> 50 < 60 -> not ready
      token_trigger_ratio=0.4 -> 50 >= 40 -> ready
    """
    from nanobot.channels.telegram import history_panel as hp

    def fake_settings(tok_trigger: float):
        return {
            "context_window_tokens": 1000,
            "tool_result_max_chars": 5000,
            "tool_results": {
                "summarize_enabled": True, "summarize_threshold": 8000,
                "exec_max_chars": 10000, "web_fetch_max_chars": 8000,
                "web_search_max_chars": 5000, "summarize_model": "x",
            },
            "history": {
                "max_messages": 100, "max_context_chars": 100_000,
                "compress_ratio": 0.99, "compress_max_summary_tokens": 600,
                "compression_keep_recent": 6, "keep_user_messages": True,
                "history_summarize_enabled": True,
                "token_trigger_ratio": tok_trigger, "context_budget_ratio": 0.65,
                "compress_fallback_chars": 2000,
            },
            "context_pruning": {"soft_ratio": 0.55, "keep_recent": 4, "soft_max_chars": 8000},
            "tool_limits": {}, "tool_log_preview": {},
        }

    # 500 tokens of a 1000-token window -> tok_pct = 50; engine=None -> 0 msgs.
    monkeypatch.setattr(hp, "_estimate_history_tokens", lambda msgs: 500)

    monkeypatch.setattr(hp, "_settings", lambda: fake_settings(0.6))
    m_high = hp.collect_live_metrics(None)
    assert m_high["compress_ready"] is False, "50% should be under a 60% trigger"

    monkeypatch.setattr(hp, "_settings", lambda: fake_settings(0.4))
    m_low = hp.collect_live_metrics(None)
    assert m_low["compress_ready"] is True, "50% should breach a 40% trigger"

