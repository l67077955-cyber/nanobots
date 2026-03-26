"""Tests for the two-phase context pruning module."""

from nanobot.agent.context_pruning import prune_messages


def _make_messages(tool_content_sizes: list[int], recent_assistants: int = 3) -> list[dict]:
    """Build a message list with system + user + (tool + assistant) pairs."""
    msgs: list[dict] = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ]
    for i, size in enumerate(tool_content_sizes):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call_{i}", "function": {"name": "test", "arguments": "{}"}}
        ]})
        msgs.append({"role": "tool", "content": "x" * size, "tool_call_id": f"call_{i}"})
        msgs.append({"role": "assistant", "content": f"Result {i}"})
    return msgs


def test_no_pruning_below_soft_ratio():
    """Messages below 30% of context window should not be pruned."""
    msgs = _make_messages([100, 200, 300])
    result = prune_messages(msgs, context_window_tokens=1_000_000)
    assert result is msgs  # same object, not modified


def test_soft_trim_preserves_head_tail():
    """Large tool results should be trimmed to head + tail."""
    # Create a tool result larger than soft_max_chars (4000)
    big_content = "H" * 5000 + "T" * 5000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_0", "function": {"name": "test", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": big_content, "tool_call_id": "call_0"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
        # 3 more assistant messages to protect
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
        {"role": "assistant", "content": "a3"},
    ]
    # Set a small context window so ratio > 0.3
    result = prune_messages(msgs, context_window_tokens=5_000)
    tool_msg = result[3]
    assert len(tool_msg["content"]) < len(big_content)
    assert tool_msg["content"].startswith("H" * 1500)
    assert "trimmed" in tool_msg["content"]


def test_hard_clear_replaces_with_placeholder():
    """When ratio exceeds hard_ratio, tool results should be replaced with placeholder."""
    big_content = "X" * 50_000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_0", "function": {"name": "test", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": big_content, "tool_call_id": "call_0"},
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
        {"role": "assistant", "content": "a3"},
    ]
    # Very small window to trigger hard clear
    result = prune_messages(msgs, context_window_tokens=1_000)
    tool_msg = result[3]
    assert tool_msg["content"] == "[Old tool result cleared]"


def test_recent_assistant_messages_protected():
    """Tool results near recent assistant messages should not be pruned."""
    big_content = "Y" * 10_000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        # Recent pair — should be protected
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_0", "function": {"name": "test", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": big_content, "tool_call_id": "call_0"},
        {"role": "assistant", "content": "result"},
    ]
    # Only 1 assistant message — can't find 3 to protect, so cutoff=0
    result = prune_messages(msgs, context_window_tokens=500)
    # Nothing should be pruned because everything is in the "protected" zone
    tool_msg = result[3]
    assert tool_msg["content"] == big_content


def test_original_messages_not_mutated():
    """The original messages list should not be mutated."""
    big_content = "Z" * 10_000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_0", "function": {"name": "test", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": big_content, "tool_call_id": "call_0"},
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
        {"role": "assistant", "content": "a3"},
        {"role": "assistant", "content": "a4"},
    ]
    original_content = msgs[3]["content"]
    result = prune_messages(msgs, context_window_tokens=500)
    # Original should be unchanged
    assert msgs[3]["content"] == original_content
    # Result should be different
    assert result[3]["content"] != original_content


def test_empty_messages():
    result = prune_messages([], context_window_tokens=100_000)
    assert result == []
