"""Tests for tiered history message pruning."""

from __future__ import annotations

from nanobot.core.history import (
    CHATROOM_TOOL_NAMES,
    _COMPRESS_HEADER,
    degrade_content,
    fit_messages_to_tier_budget,
    strip_chatroom_tool_lines,
    trim_sender_history,
    History,
)


def history_to_messages(history, current_agent="", max_chars=0, **kwargs):
    """Helper wrapper for tests."""
    return History.from_sender_dicts(history).build_for_groupchat(
        current_agent=current_agent,
        max_chars=max_chars,
    )


def _tool_block(*lines: str) -> str:
    body = "\n".join(lines)
    return f"\n\n<previous_tool_calls>\n{body}\n</previous_tool_calls>\n"


def test_strip_chatroom_tool_lines_keeps_substantive_tools():
    content = (
        "已定位问题"
        + _tool_block(
            "• chatroom_send(hello) → OK",
            "• exec(grep foo) → found bar (500字)",
            "• wait(60) → timeout",
        )
    )
    trimmed = strip_chatroom_tool_lines(content)
    assert "chatroom_send" not in trimmed
    assert "wait" not in trimmed
    assert "exec(grep foo)" in trimmed
    assert "已定位问题" in trimmed


def test_degrade_levels_follow_priority_order():
    content = "结论" + _tool_block("• chatroom_send(x) → OK", "• read_file(a) → " + ("y" * 80))
    lvl1 = degrade_content(content, 1)
    lvl2 = degrade_content(content, 2)
    lvl3 = degrade_content(content, 3)
    assert "chatroom_send" not in lvl1
    assert "read_file" in lvl1
    assert "<previous_tool_calls>" in lvl2
    assert "结论" in lvl3
    assert "<previous_tool_calls>" not in lvl3


def test_user_messages_survive_budget_pressure():
    history = [
        {"sender": "系统", "content": "话题：test"},
        {"sender": "用户", "content": "必须保留的用户澄清"},
        {
            "sender": "Harper",
            "content": "旧分析" + _tool_block("• chatroom_send(x) → OK", "• read_file(a.py) → code"),
        },
        {"sender": "用户", "content": "第二条用户消息也要留"},
        {"sender": "Kirk", "content": "可以丢掉的队友消息" + ("。" * 40)},
    ]
    trimmed = trim_sender_history(history, max_chars=70, protected_indices={0})
    users = [m["content"] for m in trimmed if m["sender"] == "用户"]
    assert "必须保留的用户澄清" in users
    assert "第二条用户消息也要留" in users
    assert all(m["sender"] != "Kirk" for m in trimmed)


def test_newer_agent_message_preferred_over_older():
    history = [
        {"sender": "用户", "content": "需求"},
        {"sender": "Harper", "content": "旧消息" + ("A" * 120)},
        {"sender": "Kirk", "content": "最新消息：Telegram按钮"},
    ]
    trimmed = trim_sender_history(history, max_chars=80, protected_indices={0})
    texts = [m["content"] for m in trimmed if m["sender"] != "用户"]
    assert any("Telegram按钮" in t for t in texts)
    assert not any(m["sender"] == "Harper" for m in trimmed)


def test_chatroom_tools_stripped_before_substantive_tools():
    history = [
        {"sender": "用户", "content": "修按钮"},
        {
            "sender": "Harper",
            "content": "排查中"
            + _tool_block(
                "• chatroom_send(task) → OK",
                "• read_file(cb.py) → " + ("x" * 30),
            ),
        },
    ]
    trimmed = trim_sender_history(history, max_chars=130, protected_indices={0})
    harper = next(m for m in trimmed if m["sender"] == "Harper")
    assert "chatroom_send" not in harper["content"]
    assert "read_file(cb.py)" in harper["content"]


def test_history_to_messages_protects_human_user_not_teammate():
    history = [
        {"sender": "系统", "content": "话题"},
        {"sender": "用户", "content": "用户原始需求"},
        {
            "sender": "Harper",
            "content": "队友汇报" + _tool_block("• chatroom_send(x) → OK"),
        },
        {"sender": "Kirk", "content": "另一条队友消息" + ("x" * 100)},
    ]
    msgs = history_to_messages(history, current_agent="Kirk", max_chars=100)
    human = [m for m in msgs if m["role"] == "user" and m["content"] == "用户原始需求"]
    assert len(human) == 1
    assert any("用户原始需求" in m.get("content", "") for m in msgs)
    assert not any(
        m.get("content", "").startswith("另一条队友")
        for m in msgs
        if m.get("role") == "assistant"
    )


def test_history_to_messages_degrades_tool_logs_before_agent_text():
    long_exec = "• exec(grep) → " + ("output " * 80) + " (3000字)"
    history = [
        {"sender": "用户", "content": "keep me"},
        {"sender": "Harper", "content": "结论摘要" + _tool_block(long_exec, "• chatroom_send(a) → OK")},
    ]
    msgs = history_to_messages(history, current_agent="Harper", max_chars=200)
    harper_msgs = [m for m in msgs if m.get("role") == "assistant" or "[Harper]" in m.get("content", "")]
    assert harper_msgs
    combined = "\n".join(m["content"] for m in harper_msgs if isinstance(m.get("content"), str))
    assert "chatroom_send" not in combined
    assert "结论摘要" in combined


def test_fit_messages_keeps_recent_optional_messages():
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "[A]: old"},
        {"role": "user", "content": "[B]: new-info"},
    ]

    def _mandatory(msg, idx):
        return idx == 0 or msg["content"] == "u1"

    fitted, skipped = fit_messages_to_tier_budget(messages, 30, is_mandatory=_mandatory)
    bodies = [m["content"] for m in fitted]
    assert "u1" in bodies
    assert any("new-info" in b for b in bodies)
    assert skipped == 0

    # Tighter budget: keep recent optional, compress/drop older one.
    fitted_tight, skipped_tight = fit_messages_to_tier_budget(
        messages, 20, is_mandatory=_mandatory,
    )
    tight_bodies = [m["content"] for m in fitted_tight]
    assert "u1" in tight_bodies
    assert any(_COMPRESS_HEADER in b for b in tight_bodies)
    assert skipped_tight >= 1


def test_omitted_messages_become_compress_block():
    history = [
        {"sender": "用户", "content": "必须保留"},
        {"sender": "Harper", "content": "旧结论 A" + ("x" * 80)},
        {"sender": "Kirk", "content": "最新结论 B"},
    ]
    trimmed = trim_sender_history(history, max_chars=60, protected_indices={0})
    assert any(m["sender"] == "用户" and "必须保留" in m["content"] for m in trimmed)
    compressed = [m for m in trimmed if _COMPRESS_HEADER in m.get("content", "")]
    assert compressed, "dropped messages should be summarized, not silently lost"
    assert "旧结论 A" in compressed[0]["content"] or "Harper" in compressed[0]["content"]


def test_full_compress_when_budget_still_exceeded():
    history = [
        {"sender": "用户", "content": "u"},
        {"sender": "A", "content": "aaa" + ("X" * 200)},
        {"sender": "B", "content": "bbb" + ("Y" * 200)},
    ]

    def _mandatory(msg, idx):
        return msg.get("sender") == "用户"

    fitted, skipped = fit_messages_to_tier_budget(
        history, 120, is_mandatory=_mandatory, sender_format=True,
    )
    assert sum(len(m.get("content", "")) for m in fitted) <= 120
    assert any(_COMPRESS_HEADER in m.get("content", "") for m in fitted)
    assert not any(m.get("sender") == "A" for m in fitted)
    assert not any(m.get("sender") == "B" for m in fitted)
    assert skipped >= 2


def test_omitted_never_silently_dropped_on_tight_budget():
    history = [
        {"sender": "用户", "content": "keep"},
        {"sender": "Harper", "content": "要压缩的旧消息" + ("x" * 120)},
    ]
    trimmed = trim_sender_history(history, max_chars=40, protected_indices={0})
    assert any(m.get("sender") == "用户" for m in trimmed)
    assert any(_COMPRESS_HEADER in m.get("content", "") for m in trimmed)


def test_chatroom_tool_names_cover_coordination_tools():
    assert "chatroom_send" in CHATROOM_TOOL_NAMES
    assert "wait" in CHATROOM_TOOL_NAMES
    assert "end_discussion" in CHATROOM_TOOL_NAMES