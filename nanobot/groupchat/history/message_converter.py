"""History → LLM messages conversion with smart truncation.

Handles both groupchat (sender) and LLM (role) input formats.
Independent of prompt construction."""

from __future__ import annotations

from typing import Any

from nanobot.groupchat.history.context_validator import validate_context


def history_to_messages(
    history: list[dict],
    current_agent: str = "",
    max_chars: int = 0,
    pin_first_user: bool = True,          # 保留參數，向後相容
    relevant_agents: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert history dicts into LLM API messages.

    新策略（2026.4 推薦）：
    - 自動偵測輸入格式：支援 groupchat (sender) 和 LLM (role) 兩種
    - 強制保留：第1條消息 + 所有 user 消息 + 所有 system 消息
    - 僅在中間裁剪 assistant 消息，從尾部補齊最近對話
    - 單 Agent 模式現在也會走此邏輯
    """
    def _to_msg(m: dict[str, str]) -> dict[str, Any]:
        sender, content = m["sender"], m["content"]
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
    critical: list[dict[str, Any]] = []
    for i, m in enumerate(msgs_full):
        if m["role"] in ("user", "system") or i == 0:
            critical.append(m)

    # ── 預算計算 ──
    used_chars = sum(len(m.get("content", "")) for m in critical)
    budget = max_chars - used_chars

    # ── 從尾部補齊 ──
    tail: list[dict[str, Any]] = []
    for m in reversed(msgs_full):
        if m in critical:
            continue
        c = len(m.get("content", ""))
        if budget - c < 0:
            break
        tail.insert(0, m)
        budget -= c

    # ── 重建最終列表（保持時序） ──
    result: list[dict[str, Any]] = []
    seen = set()
    for m in msgs_full:
        m_id = id(m)
        if (m in critical or m in tail) and m_id not in seen:
            result.append(m)
            seen.add(m_id)

    # ── 插入省略提示 ──
    skipped = len(msgs_full) - len(result)
    if skipped > 0:
        insert_pos = 0
        result.insert(insert_pos, {
            "role": "system",
            "content": "[...earlier messages omitted...]",
        })

    # ── 自检: 上下文完整性验证 ──
    validate_context(result, current_agent, skipped)

    return result
