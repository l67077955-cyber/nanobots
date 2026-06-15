"""History → LLM messages conversion with smart truncation.

Handles both groupchat (sender) and LLM (role) input formats.
Independent of prompt construction."""

from __future__ import annotations

import re
from typing import Any

from nanobot.groupchat.history.context_validator import validate_context

# ── Tool Log Aging ──────────────────────────────────────────────────────────

_TOOL_LOG_RE = re.compile(
    r"(• \w+\([^)]*\) → )"
    r"([^\n]*?)"       # preview text bounded to single line (non-greedy, no cross-line)
    r"(\(\d[\d,]*字\))",  # trailing char count
    # NOTE: removed re.DOTALL to prevent (.*?) from spanning across line boundaries
    # and accidentally consuming char counts from subsequent tool call lines
)

# Matches tool call history blocks (old [工具调用记录] or new <previous_tool_calls>)
# We support both for history compatibility during transition.
_TOOL_LOG_BLOCK_RE = re.compile(
    r"\n*(?:\[工具调用记录\]|<previous_tool_calls>[\s\S]*?</previous_tool_calls>).*$",
    re.DOTALL,
)


def age_tool_log(content: str) -> str:
    """Compress tool call previews to first-line summary.

    Transforms:
      • exec(grep 'foo' /tmp/) → STDERR: No such file or directory\nExit code: 1 (112字)
    Into:
      • exec(grep 'foo' /tmp/) → STDERR: No such file or directory (112字)

    Keeps first line of preview (max 100 chars) so agents see what happened,
    without the verbose multi-line output.

    Returns the content unchanged if no tool log block is found.
    """
    if "[工具调用记录]" not in content and "<previous_tool_calls>" not in content:
        return content

    def _replace(m: re.Match) -> str:
        preview = m.group(2).strip()
        first_line = preview.split("\n")[0][:100]
        if first_line:
            return f"{m.group(1)}{first_line} {m.group(3)}"
        return m.group(1) + m.group(3)

    return _TOOL_LOG_RE.sub(_replace, content)


def strip_tool_log(content: str) -> str:
    """Remove the entire tool call history block (<previous_tool_calls> or legacy [工具调用记录])
    from a message.

    Used for rank-based context isolation: high-rank agents (Leader)
    don't need to see low-rank agents' tool call details.
    """
    return _TOOL_LOG_BLOCK_RE.sub("", content)


def history_to_messages(
    history: list[dict],
    current_agent: str = "",
    max_chars: int = 0,
    pin_first_user: bool = True,          # 保留參數，向後相容
    relevant_agents: list[str] | None = None,
    agent_ranks: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Convert history dicts into LLM API messages.

    新策略（2026.4 推薦）：
    - 自動偵測輸入格式：支援 groupchat (sender) 和 LLM (role) 兩種
    - 強制保留：第1條消息 + 所有 user 消息 + 所有 system 消息
    - 僅在中間裁剪 assistant 消息，從尾部補齊最近對話
    - 單 Agent 模式現在也會走此邏輯

    Rank-based tool call isolation (2026.5):
    - agent_ranks: {agent_name: rank_int} — higher number = higher privilege
    - An agent can only see tool calls from agents with rank <= its own rank
    - Text messages are never stripped, only tool history blocks
      (<previous_tool_calls> or legacy [工具调用记录])
    """
    from nanobot.groupchat.display.visibility import can_see_tool_call

    my_rank = (agent_ranks or {}).get(current_agent, 0)

    def _to_msg(m: dict[str, str]) -> dict[str, Any]:
        sender, content = m["sender"], m["content"]
        # Rank-based tool call isolation: strip tool logs from lower-rank agents
        has_tool_log = "[工具调用记录]" in content or "<previous_tool_calls>" in content
        if agent_ranks and sender not in ("用户", "系统", current_agent):
            sender_rank = agent_ranks.get(sender, 0)
            if not can_see_tool_call(sender_rank, my_rank) and has_tool_log:
                content = strip_tool_log(content)
            elif has_tool_log:
                # Compress visible cross-agent tool logs: keep fn+args+char count only
                content = age_tool_log(content)
        if sender == "用户":
            return {"role": "user", "content": content}
        elif sender == "系统":
            return {"role": "system", "content": content}
        elif sender == current_agent:
            return {
                "role": "assistant",
                "content": content,
                "name": sender.replace(" ", "_"),
            }
        else:
            return {
                "role": "user",
                "content": f"[{sender}]: {content}",
                "name": sender.replace(" ", "_"),
            }

    if not history:
        return []

    # ── 自動偵測格式 ──
    first = history[0]
    is_groupchat = "sender" in first

    allowed = {"用户", "系统"} | set(relevant_agents or [])
    msgs_full = history if not is_groupchat else [
        _to_msg(m) for m in history
        if relevant_agents is None or m["sender"] in allowed
    ]

    if not max_chars or not msgs_full:
        return msgs_full

    # ── 強制保留核心消息 ──
    # Protect index 0 (system prompt) + ALL user messages + ALL system messages.
    # User messages are always important — they contain the human's intent.
    # Only assistant messages are trimmed when budget is tight.
    critical: list[dict[str, Any]] = []
    for i, m in enumerate(msgs_full):
        if i == 0:
            critical.append(m)
        elif m["role"] in ("user", "system"):
            critical.append(m)

    # ── 預算計算 ──
    used_chars = sum(len(m.get("content", "")) for m in critical)
    # Reserve space for the omission prompt that will be inserted if messages are skipped
    _OMISSION_PROMPT = "[...earlier messages omitted...]"
    budget = max_chars - used_chars - len(_OMISSION_PROMPT)

    # ── 從尾部補齊（先嘗試 aging 中間消息的 tool log） ──
    tail: list[dict[str, Any]] = []
    aged_map: dict[int, dict[str, Any]] = {}  # original id → aged message
    for m in reversed(msgs_full):
        if m in critical:
            continue
        c = len(m.get("content", ""))
        if budget - c < 0:
            # Try aging tool logs to fit
            aged_content = age_tool_log(m.get("content", ""))
            aged_c = len(aged_content)
            if aged_c < c and budget - aged_c >= 0:
                aged_map[id(m)] = {**m, "content": aged_content}
                budget -= aged_c
                continue
            break
        tail.insert(0, m)
        budget -= c

    # ── 重建最終列表（保持時序） ──
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for m in msgs_full:
        m_id = id(m)
        if m_id in seen:
            continue
        if m in critical or m in tail:
            result.append(m)
            seen.add(m_id)
        elif m_id in aged_map:
            result.append(aged_map[m_id])
            seen.add(m_id)

    # ── 插入省略提示 ──
    skipped = len(msgs_full) - len(result)
    if skipped > 0:
        insert_pos = 0
        result.insert(insert_pos, {
            "role": "system",
            "content": _OMISSION_PROMPT,
        })

    # ── 自检: 上下文完整性验证 ──
    validate_context(result, current_agent, skipped)

    return result
