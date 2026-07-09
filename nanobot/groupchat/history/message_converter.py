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
from nanobot.core.history import History

__all__ = [
    "CHATROOM_TOOL_NAMES",
    "_COMPRESS_HEADER",
    "age_tool_log",
    "build_compress_message",
    "can_see_tool_call",
    "degrade_content",
    "fit_messages_to_tier_budget",
    "has_tool_log",
    "history_to_messages",
    "strip_chatroom_tool_lines",
    "strip_tool_log",
    "trim_llm_messages",
    "trim_sender_history",
]


def history_to_messages(
    history: list[dict],
    current_agent: str = "",
    max_chars: int = 0,
    pin_first_user: bool = True,
    relevant_agents: list[str] | None = None,
    agent_ranks: dict[str, int] | None = None,
) -> list[dict]:
    """Convert sender-dict history to LLM messages via History.build_for_groupchat."""
    hist_obj = History.from_sender_dicts(history)
    rel_set = set(relevant_agents) if relevant_agents is not None else None
    return hist_obj.build_for_groupchat(
        current_agent=current_agent,
        agent_ranks=agent_ranks,
        relevant_agents=rel_set,
        max_chars=max_chars,
    )
