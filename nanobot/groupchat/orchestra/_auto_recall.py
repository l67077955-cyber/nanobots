"""Pre-discussion automatic memory recall.

Called before broadcast_round starts. Runs 3 parallel wing-filtered recalls:
  1. wing_code  — technical context, tools, methods, pitfalls
  2. wing_user  — user preferences, output style, interaction habits
  3. wing_agent — facts, discoveries, rules, cross-task knowledge

Results are concatenated and injected as a system message into history
so all agents see them in the upcoming broadcast round.
"""

from __future__ import annotations

from loguru import logger

# ── 3 recall wings ───────────────────────────────────────────

_RECALL_WINGS: list[tuple[str, str, str]] = [
    ("wing_code", "技术上下文", "工具用法/方法/踩坑/技术结论"),
    ("wing_user", "用户偏好", "输出风格/交互习惯/格式偏好"),
    ("wing_agent", "固定信息", "事实数据/发现/规则约定"),
]

_MAX_CHARS_PER_WING = 2000  # truncate per-wing output to avoid bloating history


def _recall_wing(wing: str, n_results: int = 5) -> str:
    """Recall drawers from a single wing via MemoryStack.

    Returns formatted string (empty if wing has no drawers or on error).
    """
    try:
        from mempalace.layers import MemoryStack  # noqa: PLC0415

        stack = MemoryStack()
        result = stack.recall(wing=wing, n_results=n_results)
        if not result or "0 drawers" in result.split("\n")[0]:
            return ""
        # Truncate if too long
        if len(result) > _MAX_CHARS_PER_WING:
            result = result[:_MAX_CHARS_PER_WING] + "\n... (truncated)"
        return result
    except Exception as e:
        logger.warning("auto_recall: failed to recall wing={}: {}", wing, e)
        return ""


async def auto_recall_memories(user_input: str = "") -> str:
    """Recall memories from all 3 wings and format for injection.

    Returns a formatted string suitable for engine._add_message("系统", ...).
    Returns empty string if nothing was recalled.
    """
    sections: list[str] = []

    for wing, label, desc in _RECALL_WINGS:
        content = _recall_wing(wing)
        if content:
            sections.append(f"### {label} ({wing}) — {desc}\n{content}")

    if not sections:
        return ""

    header = "## 🧠 自动记忆检索（群聊开始时触发）"
    if user_input:
        header += f"\n当前用户输入: {user_input[:200]}"

    return header + "\n\n" + "\n\n---\n\n".join(sections)
