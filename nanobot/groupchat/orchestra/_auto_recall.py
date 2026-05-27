"""Pre-discussion automatic memory recall.

Called before broadcast_round starts. Generates wing-specific search keywords
from user_input via LLM, then runs 3 parallel wing-filtered semantic searches:
  1. wing_code  — technical context, tools, methods, pitfalls
  2. wing_user  — user preferences, output style, interaction habits
  3. wing_agent — facts, discoveries, rules, cross-task knowledge

Uses L3.search (semantic) so results are relevant to the current user input.
Each wing gets its own tailored query instead of reusing the raw user_input.

Results are concatenated and injected as a system message into history
so all agents see them in the upcoming broadcast round.
"""

from __future__ import annotations

import json
import re

from loguru import logger

# ── 3 recall wings ───────────────────────────────────────────

_RECALL_WINGS: list[tuple[str, str, str]] = [
    ("wing_code", "技术上下文", "工具用法/方法/踩坑/技术结论"),
    ("wing_user", "用户偏好", "输出风格/交互习惯/格式偏好"),
    ("wing_agent", "固定信息", "事实数据/发现/规则约定"),
]

_MAX_CHARS_PER_WING = 2000  # truncate per-wing output to avoid bloating history

_KEYWORD_PROMPT = """\
用户输入: "{user_input}"

请为以下3个记忆仓库各生成一组检索关键词（中英文+同义词，每组合并为一行）：

1. wing_code（技术上下文：工具用法、方法、踩坑、技术结论）
2. wing_user（用户偏好：输出风格、交互习惯、格式偏好）
3. wing_agent（固定信息：事实数据、发现、规则约定）

只返回JSON，不要其他内容：
{{"wing_code": "关键词1 关键词2 ...", "wing_user": "...", "wing_agent": "..."}}"""


def _recall_wing(wing: str, query: str = "", n_results: int = 5) -> str:
    """Recall drawers from a single wing via MemoryStack.

    Uses L3.search (semantic) when query is provided, falls back to
    L2.recall (insertion-order) when query is empty.

    Returns formatted string (empty if wing has no drawers or on error).
    """
    try:
        from mempalace.layers import MemoryStack  # noqa: PLC0415

        stack = MemoryStack()
        if query:
            result = stack.search(query=query, wing=wing, n_results=n_results)
        else:
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


async def _generate_wing_queries(user_input: str, engine) -> dict[str, str]:
    """Use LLM to generate wing-specific search keywords from user_input.

    Returns dict like {"wing_code": "...", "wing_user": "...", "wing_agent": "..."}.
    Falls back to raw user_input for all wings on failure.
    """
    try:
        from nanobot.groupchat.history.history_settings import (
            summarize_model as _get_summarize_model,
        )

        model = _get_summarize_model()
        provider = getattr(engine, "provider", None)
        if not provider:
            return {w: user_input for w, _, _ in _RECALL_WINGS}

        prompt = _KEYWORD_PROMPT.format(user_input=user_input[:500])
        result = await provider.chat_with_retry(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=300,
            temperature=0.3,
        )

        # Extract JSON from response
        text = result.strip()
        match = re.search(r"\{[^}]+\}", text, re.DOTALL)
        if not match:
            return {w: user_input for w, _, _ in _RECALL_WINGS}

        queries = json.loads(match.group())
        # Validate keys
        valid = {w: user_input for w, _, _ in _RECALL_WINGS}
        for w, _, _ in _RECALL_WINGS:
            if w in queries and isinstance(queries[w], str) and queries[w].strip():
                valid[w] = queries[w].strip()

        logger.info("auto_recall: generated wing queries: {}", valid)
        return valid

    except Exception as e:
        logger.warning("auto_recall: failed to generate wing queries: {}", e)
        return {w: user_input for w, _, _ in _RECALL_WINGS}


async def auto_recall_memories(user_input: str = "", engine=None) -> str:
    """Recall memories from all 3 wings and format for injection.

    Generates wing-specific search keywords from user_input via LLM,
    then uses each wing's tailored query for semantic search.

    Returns a formatted string suitable for engine._add_message("系统", ...).
    Returns empty string if nothing was recalled.
    """
    sections: list[str] = []

    # Generate wing-specific queries if engine is available
    if engine and user_input:
        wing_queries = await _generate_wing_queries(user_input, engine)
    else:
        wing_queries = {w: user_input for w, _, _ in _RECALL_WINGS}

    for wing, label, desc in _RECALL_WINGS:
        query = wing_queries.get(wing, user_input)
        content = _recall_wing(wing, query=query)
        if content:
            sections.append(f"### {label} ({wing}) — {desc}\n{content}")

    if not sections:
        return ""

    header = "## 🧠 自动记忆检索（群聊开始时触发）"
    if user_input:
        header += f"\n当前用户输入: {user_input[:200]}"

    return header + "\n\n" + "\n\n---\n\n".join(sections)
