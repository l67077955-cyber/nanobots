"""History → LLM messages conversion utilities.

Re-exports from nanobot.core.history for backward compatibility.
All algorithms are canonical in core/history.py (dependency inversion).
"""

from __future__ import annotations

from nanobot.core.history import (
    _COMPRESS_HEADER,
    _TOOL_LINE_RE,
    CHATROOM_TOOL_NAMES,
    _merge_consecutive_assistant,
    age_tool_log,
    build_compress_message,
    can_see_tool_call,
    degrade_content,
    fit_messages_to_tier_budget,
    has_tool_log,
    strip_chatroom_tool_lines,
    strip_tool_log,
    trim_llm_messages,
    trim_sender_history,
)

__all__ = [
    "CHATROOM_TOOL_NAMES",
    "_COMPRESS_HEADER",
    "age_tool_log",
    "build_compress_message",
    "can_see_tool_call",
    "degrade_content",
    "fit_messages_to_tier_budget",
    "has_tool_log",
    "strip_chatroom_tool_lines",
    "strip_tool_log",
    "trim_llm_messages",
    "trim_sender_history",
]
