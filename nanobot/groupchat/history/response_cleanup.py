"""Response cleanup utilities for group chat.

Pure functions for stripping model artifacts from LLM responses:
think blocks, fake tool calls, agent name prefixes, etc.
"""

from __future__ import annotations

import re


def clean_response(content: str, agent_name: str, all_agent_names: list[str]) -> str:
    """Clean up model response: strip think blocks, name prefixes, fake tool calls.

    Args:
        content: Raw LLM response text.
        agent_name: The responding agent (for targeted cleanup).
        all_agent_names: All registered agent names (for prefix stripping).

    Returns:
        Cleaned response string.
    """
    # 1. Strip <think>...</think> blocks (deepseek, some models)
    content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()

    # 2. Strip fake/hallucinated tool calls in text
    # Qwen/NIM style: <|tool_calls_section_begin|>...<|tool_calls_section_end|>
    content = re.sub(
        r"<\|tool_calls_section_begin\|>[\s\S]*?<\|tool_calls_section_end\|>",
        "", content,
    )
    # Also strip any stray <|...|> model delimiters
    content = re.sub(r"<\|[a-z_]+\|>", "", content)
    # Bracket style: [Start search for ...], [Check ...], [调用 exec({...})], etc.
    content = re.sub(r"\[(?:Start |Check |Search |Look up |Fetch |查|搜|调用\s*\w+)[^\]]*\]", "", content)
    # XML style: <function_calls>...</function_calls>, <web_search>...</web_search>, etc.
    _TAG_NAMES = (
        "function_calls|invoke|web_search|web_fetch|tool|parameter|query|"
        "search|tool_call|parameters|freshness|count"
    )
    content = re.sub(
        rf"<(?:{_TAG_NAMES})[\s\S]*?(?:</(?:{_TAG_NAMES})>|$)",
        "", content, flags=re.IGNORECASE,
    )
    # Stray closing tags left behind
    content = re.sub(
        rf"</(?:{_TAG_NAMES})>",
        "", content, flags=re.IGNORECASE,
    )

    # 4. Strip ALL agent name prefixes (handles repeated "Benjamin: ..." throughout)
    for name in all_agent_names:
        for sep in (": ", "：", ":\n"):
            prefix = f"{name}{sep}"
            content = content.replace(prefix, "")
        # Also strip markdown headers like "# Benjamin" or "## Benjamin"
        content = re.sub(rf"^#+\s*{re.escape(name)}\s*$", "", content, flags=re.MULTILINE)

    # 5. Clean up excessive blank lines left after stripping
    content = re.sub(r"\n{3,}", "\n\n", content)

    return content.strip()
