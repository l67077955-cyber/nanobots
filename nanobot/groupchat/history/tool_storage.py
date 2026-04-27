"""Storage management for massive tool results.

Ensures that arbitrarily large tool returns (like aggressive curl scraping or
deep log reading) do not immediately crash the API context limits. Large results
are persisted securely to disk, and a heavily truncated summary snippet is
passed back to the LLM.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger

# Store persisted long-outputs here
STORAGE_DIR = Path("/tmp/nanobot/tool_storage")
_MAX_LLM_CHARS = 6_000


def maybe_persist_tool_result(
    content: str, 
    tool_name: str, 
    tool_call_id: str,
    max_chars: int = _MAX_LLM_CHARS
) -> str:
    """Intercept and persist large tool results to disk to prevent context bloat.
    
    If the content exceeds `max_chars`, the full string is written to
    a cache directory, and a safely truncated string is returned for the LLM payload.

    Args:
        content: The raw string output from the executed tool.
        tool_name: Name of the executed tool.
        tool_call_id: The unique tool_call_id string.
        max_chars: Threshold for truncation. Defaults to 10k chars.

    Returns:
        The safe string to insert into LLM messages.
    """
    if not isinstance(content, str):
        return content

    length = len(content)
    if length <= max_chars:
        return content

    # Result is exceptionally large. Ensure storage dir exists and persist.
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    file_path = STORAGE_DIR / f"{tool_call_id}.txt"
    
    try:
        file_path.write_text(content, encoding="utf-8")
        logger.info(
            "history.tool_storage: Persisted massive tool result ({}) of {:,} chars to disk at {}",
            tool_name, length, file_path
        )
    except Exception as e:
        logger.error("history.tool_storage: Failed to persist massive tool result: {}", e)
        # If cache fails we still truncate to prevent API crash.
        pass

    snippet = content[:max_chars - 500]
    
    # Return a visibly truncated representation that informs LLM of what happened.
    template = (
        f"{snippet}\n\n"
        f"...\n"
        f"[截断] 该工具的返回结果极长 ({length:,} chars)，已自动截断以保护内存。\n"
        f"完整结果已落盘至: {file_path}"
    )
    
    return template
