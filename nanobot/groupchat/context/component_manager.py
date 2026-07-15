"""System warning templates for idle/timeout enforcement + synthesis quality checks."""

import re


# ── System Warning Templates ──────────────────────────────────

SYSTEM_WARNINGS: dict[str, str] = {
    "idle": (
        "[⚠️ 你（{name}）还没有采取任何行动！]\n"
        "你必须立即使用工具（web_search, web_fetch, exec 等）来回答用户的最新问题。\n"
        "不要直接从之前的对话中回答 — 用户需要新的搜索结果。\n"
        "禁止调用 wait() — 先执行工作再交流。"
    ),
    "no_text_after_tools": (
        "[⚠️ 你（{name}）完成了工具调用，但没有输出任何文字！]\n"
        "请用自然语言总结工具执行结果，写出你的结论，让 Leader 和队友能看到你的输出。\n"
        "禁止再调用工具，直接输出文字。"
    ),
    "leader_no_text_after_tools": (
        "[⚠️ 你（{name}）完成了管理操作，但没有输出任何文字！]\n"
        "请立即整合所有队友的发现，给出完整、结构化的最终答案。\n"
        "这是你作为 Leader 的核心职责，禁止再调用工具，直接输出文字。"
    ),
    "leader_end_without_text": (
        "[⚠️ 你调用了 end_discussion，但还没有给出最终答案！]\n"
        "请立即整合所有队友的发现，给出完整、结构化的最终回复给用户。\n"
        "这是用户唯一能看到的内容，禁止再调用任何工具，直接输出文字。"
    ),
    "leader_wait_timeout": (
        "[最终综合] 等待超时，队友已全部完成。\n"
        "请立即综合所有发现，给出完整、结构化的最终答案给用户。\n"
        "禁止再调用工具，直接输出文字。"
    ),
    "delivery_gate_memory": (
        "[⚠️ 交付门控拦截] 你将数据存入了 memory_palace 但未在文本回复中呈现。\n"
        "用户看不到 memory 的内容——唯一交付通道是你的文本回复。\n"
        "请将存储的关键数据完整写入文本，不要只写'已存储'或'已完成'。"
    ),
    "delivery_gate_tools": (
        "[⚠️ 交付门控拦截] 你使用了数据采集工具但回复中未包含对应的具体数据。\n"
        "搜索/抓取/执行的结果只存在于你的上下文中，用户看不到。\n"
        "请将关键发现、数据、URL 写入文本回复。"
    ),
}


def get_system_warning(kind: str, **kwargs) -> str:
    """Retrieve a system warning template filled with kwargs."""
    tpl = SYSTEM_WARNINGS.get(kind)
    if tpl is None:
        return ""
    return tpl.format(**kwargs)


# ── Synthesis Quality Check ───────────────────────────────────

_MIN_SYNTHESIS_LEN = 400

_META_PATTERNS = [
    "问题已解答", "无需补充", "已交付", "已完成",
]


def synthesis_quality_check(text: str, tools_used: list[str] | None = None) -> tuple[bool, str]:
    """Check whether a Leader's synthesis contains substantive content.

    Four-tier heuristic:
      1. Structural markers present (## headings)?
      2. Output dominated by meta-fluff rather than real conclusions?
      3. Contains concrete evidence (data points, sources)?
      4. (NEW) Delivery Gate — substantive tools used but no corresponding data in text?

    Args:
        text: The synthesis text to check.
        tools_used: List of tool names called during this agent's run.
                    If provided, enables Tier 4 delivery gate checks.

    Returns (passes=True/False, failure_reason_if_not_pass).
    """
    if not text or not text.strip():
        return False, "总结为空"

    # Tier 1 – structured heading presence
    has_structure = bool(re.search(r'^##\s', text, re.MULTILINE))

    # Tier 2 – meta-fluff ratio
    meta_hits = sum(1 for p in _META_PATTERNS if p in text)
    meta_ratio = meta_hits / max(len(_META_PATTERNS), 1)

    if meta_ratio > 0.3 and not has_structure:
        return False, "输出主要是元信息而非实质性总结"

    # Tier 3 – concrete evidence
    has_numbers = bool(re.search(r'\d+', text))
    has_urls = bool(re.search(r'https?://', text))
    sentence_count = len(re.findall(r'[。！？\n]', text))

    if not has_structure and not has_numbers and not has_urls and sentence_count < 3:
        return False, "总结缺乏具体数据、来源或足够的内容深度"

    # Tier 4 – Delivery Gate: detect data stored but not delivered
    if tools_used:
        tools_set = set(tools_used)

        # 4a: memory_palace.store used (with visible=false) but synthesis is thin
        #     Only gate when memory_palace is the ONLY substantive tool AND text lacks evidence
        _data_tools = {"web_search", "web_fetch", "exec", "read_file"}
        has_other_data_tools = bool(tools_set & _data_tools)
        if "memory_palace" in tools_set and not has_other_data_tools:
            # memory_palace was used without any data-collection tools
            # Check if text already contains concrete data (agent may have written it inline)
            has_inline_data = (
                has_urls
                or bool(re.search(r'\d{3,}', text))
                or bool(re.search(r'```', text))
            or len(text.strip()) >= _MIN_SYNTHESIS_LEN
            )
            if not has_inline_data:
                return False, (
                    "数据存入了记忆但未在回复中呈现给用户。"
                    "请将存储的关键数据完整写入最终文本回复。"
                )

        # 4b: data-collection tools used but synthesis lacks corresponding evidence
        if tools_set & _data_tools:
            has_data_evidence = (
                has_urls  # URLs from search/fetch
                or bool(re.search(r'\d{3,}', text))  # large numbers (prices, counts, dates)
                or bool(re.search(r'```', text))  # code blocks from exec/read_file
            )
            if not has_data_evidence and not has_structure:
                return False, (
                    "使用了数据采集工具但回复中未包含对应的具体数据。"
                    "请将搜索/抓取/执行结果中的关键信息写入文本。"
                )

    return True, ""
