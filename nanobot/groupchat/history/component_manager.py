"""System warning templates for idle/timeout enforcement."""


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
}


def get_system_warning(kind: str, **kwargs) -> str:
    """Retrieve a system warning template filled with kwargs."""
    tpl = SYSTEM_WARNINGS.get(kind)
    if tpl is None:
        return ""
    return tpl.format(**kwargs)
