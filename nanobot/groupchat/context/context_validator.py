"""Context validation for LLM message lists.

Validates message ordering, tool call visibility, compression integrity.
Independent of prompt construction — pure diagnostic logic."""

from __future__ import annotations

from loguru import logger


def validate_context(
    messages: list[dict],
    agent_name: str = "",
    skipped: int = 0,
) -> list[str]:
    """Validate a message list and return any warnings found."""
    warnings: list[str] = []
    tag = f"[ctx/{agent_name or '?'}]"

    def _warn(msg: str):
        warnings.append(msg)
        logger.warning("{}", msg)

    if not messages:
        _warn(f"{tag} 上下文为空")
        return warnings

    # ── 1. 顺序检查 ──
    prev_role = None
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        if role == "assistant" and prev_role == "assistant":
            _warn(f"{tag} 顺序异常: 连续两条 assistant 消息 (index {i-1}→{i})")
        if role == "tool" and prev_role not in ("assistant", "tool"):
            _warn(f"{tag} 顺序异常: tool result 在 index {i} 前无 assistant 消息")
        prev_role = role

    # ── 2. 可见性检查: 孤立的 tool result ──
    declared_ids = {
        tc.get("id", "")
        for m in messages if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or []) if isinstance(tc, dict)
    }
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id", "")
            if tcid and tcid not in declared_ids:
                _warn(f"{tag} 可见性异常: tool result index {i} 的 tool_call_id={tcid[:12]} 找不到对应 tool_call")

    # ── 3. 压缩检查: 保留了至少一条 user 消息 ──
    if not any(m.get("role") == "user" for m in messages):
        _warn(f"{tag} 压缩异常: 上下文中没有任何 user 消息")

    # ── 4. 压缩检查: 逻辑矛盾 ──
    if skipped == 0 and any("[...earlier messages omitted...]" in (m.get("content") or "") for m in messages):
        _warn(f"{tag} 压缩异常: 存在省略提示但 skipped=0")

    # ── 5. 总字符统计 ──
    total_chars = sum(len(m.get("content") or "") for m in messages)
    logger.debug(
        "{} context ok: {} msgs, {} chars, {} skipped, {} warnings",
        tag, len(messages), total_chars, skipped, len(warnings),
    )

    return warnings
