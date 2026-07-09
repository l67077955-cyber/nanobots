"""History → LLM messages conversion with smart truncation.

Handles both groupchat (sender) and LLM (role) input formats.
Independent of prompt construction.

The dict-level truncation / degradation / compress algorithms are canonical
in ``nanobot.core.history`` (dependency inversion: core is the lower layer).
This module re-exports them for backward-compatibility and keeps the
groupchat-specific ``history_to_messages`` sender→role mapper, which is
retired once ``History.build_for_groupchat`` becomes the sole build path.
"""

from __future__ import annotations

from typing import Any

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
from nanobot.groupchat.history.context_validator import validate_context

__all__ = [
    "CHATROOM_TOOL_NAMES",
    "_COMPRESS_HEADER",
    "age_tool_log",
    "build_compress_message",
    "can_see_tool_call",
    "degrade_content",
    "fit_messages_to_tier_budget",
    "has_chatroom_tool_lines",
    "has_tool_log",
    "history_to_messages",
    "latest_user_question",
    "strip_chatroom_tool_lines",
    "strip_tool_log",
    "trim_llm_messages",
    "trim_sender_history",
]


def _tool_block_inner(content: str) -> str:
    if "<previous_tool_calls>" in content:
        start = content.index("<previous_tool_calls>")
        end = content.find("</previous_tool_calls>", start)
        if end == -1:
            return content[start:]
        return content[start : end + len("</previous_tool_calls>")]
    if "[工具调用记录]" in content:
        start = content.index("[工具调用记录]")
        return content[start:]
    return ""


def has_chatroom_tool_lines(content: str) -> bool:
    inner = _tool_block_inner(content)
    if not inner:
        return False
    return any(m.group(1) in CHATROOM_TOOL_NAMES for m in _TOOL_LINE_RE.finditer(inner))


def latest_user_question(history: list[dict]) -> str:
    """Return the latest non-summary user message (for volatile prompt tail)."""
    for msg in reversed(history):
        if msg.get("sender") in ("User", "user", "用户"):
            content = msg.get("content", "")
            if content.startswith("["):
                continue
            return content[:300]
    return ""


def history_to_messages(
    history: list[dict],
    current_agent: str = "",
    max_chars: int = 0,
    pin_first_user: bool = True,  # backward compat, unused
    relevant_agents: list[str] | None = None,
    agent_ranks: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Convert history dicts into LLM API messages."""
    my_rank = (agent_ranks or {}).get(current_agent, 0)

    def _to_msg(m: dict[str, str]) -> dict[str, Any]:
        sender, content = m["sender"], m["content"]
        has_tool = has_tool_log(content)
        if agent_ranks and sender not in ("用户", "系统", current_agent):
            sender_rank = agent_ranks.get(sender, 0)
            if not can_see_tool_call(sender_rank, my_rank) and has_tool:
                content = strip_tool_log(content)
        if sender == "用户":
            return {"role": "user", "content": content}
        if sender == "系统":
            return {"role": "system", "content": content}
        if sender == current_agent:
            return {
                "role": "assistant",
                "content": content,
                "name": sender.replace(" ", "_"),
            }
        return {
            "role": "user",
            "content": f"[{sender}]: {content}",
            "name": sender.replace(" ", "_"),
        }

    if not history:
        return []

    first = history[0]
    is_groupchat = "sender" in first

    allowed = {"用户", "系统"} | set(relevant_agents or [])
    msgs_full = (
        history
        if not is_groupchat
        else [_to_msg(m) for m in history if relevant_agents is None or m["sender"] in allowed]
    )

    if not max_chars or not msgs_full:
        return _merge_consecutive_assistant(msgs_full)

    result, skipped = trim_llm_messages(msgs_full, max_chars)
    result = _merge_consecutive_assistant(result)
    validate_context(result, current_agent, skipped)
    return result
