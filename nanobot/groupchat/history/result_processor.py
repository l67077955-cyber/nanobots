"""Unified tool result post-processing pipeline.

Single chokepoint for ALL tool results after execution.
Replaces scattered truncation in shell.py, web.py, and tool_storage.py.

Pipeline: normalize → truncate → persist → inject_meta
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from loguru import logger

STORAGE_DIR = Path("/tmp/nanobot/tool_storage")

# tool_name → (config_getter_name, strategy)
_TOOL_CONFIGS: dict[str, tuple[str, str]] = {
    "exec":       ("exec_max_chars", "head_tail"),
    "web_fetch":  ("web_fetch_max_chars", "head_only"),
    "web_search": ("web_search_max_chars", "head_only"),
}
_FALLBACK_CONFIG = ("tool_result_max_chars", "head_tail")


def _get_max_chars(tool_name: str) -> int:
    """Read per-tool max_chars from history_settings."""
    try:
        from nanobot.groupchat.history import history_settings as hs
        config_key, _ = _TOOL_CONFIGS.get(tool_name, _FALLBACK_CONFIG)
        getter = getattr(hs, config_key, None)
        if getter is not None:
            val = getter()
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
    except Exception:
        pass
    return 20_000  # safe fallback


def _truncate(text: str, max_chars: int, strategy: str) -> tuple[str, bool]:
    """Truncate text. Returns (truncated_text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    if strategy == "head_tail":
        half = max_chars // 2
        return (
            text[:half]
            + f"\n\n... ({len(text) - max_chars:,} chars truncated) ...\n\n"
            + text[-half:],
            True,
        )
    # head_only
    return text[:max_chars] + f"\n... ({len(text) - max_chars:,} chars truncated)", True


def _normalize(content: Any) -> tuple[Any, bool]:
    """Normalize content. Returns (processed, is_multimodal).

    Multimodal lists (containing image_url) pass through unchanged.
    """
    if isinstance(content, list):
        if any(isinstance(item, dict) and "image_url" in item for item in content):
            return content, True
        return json.dumps(content, ensure_ascii=False, indent=2), False
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, indent=2), False
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace"), False
    if not isinstance(content, str):
        return str(content), False
    return content, False


def _persist_to_disk(content: str, tool_name: str, tool_call_id: str) -> Path | None:
    """Persist full content to disk. Returns file path or None."""
    try:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = STORAGE_DIR / f"{tool_call_id}.txt"
        file_path.write_text(content, encoding="utf-8")
        logger.info(
            "result_processor: Persisted {} result ({:,} chars) to {}",
            tool_name, len(content), file_path,
        )
        return file_path
    except Exception as e:
        logger.error("result_processor: Failed to persist: {}", e)
        return None


def process_tool_result(
    content: Any,
    tool_name: str,
    tool_call_id: str,
    meta: dict | None = None,
) -> Any:
    """Unified post-processing for all tool results.

    Pipeline:
    1. Normalize (bytes/dict/list → str; multimodal lists pass through)
    2. Truncate (per-tool config from history_settings)
    3. Persist (if truncated, save full to disk)
    4. Inject meta footer (exit_code, url, query, etc.)

    Returns processed result ready for LLM context (str or multimodal list).
    """
    # Step 1: Normalize
    processed, is_multimodal = _normalize(content)
    if is_multimodal:
        return processed  # multimodal lists pass through unchanged

    text: str = processed

    # Step 2: Get config and truncate
    config_key, strategy = _TOOL_CONFIGS.get(tool_name, _FALLBACK_CONFIG)
    max_chars = _get_max_chars(tool_name)
    truncated_text, was_truncated = _truncate(text, max_chars, strategy)

    # Step 3: Persist full content if truncated
    if was_truncated:
        disk_path = _persist_to_disk(text, tool_name, tool_call_id)
        if disk_path:
            truncated_text += f"\n\n[完整结果已落盘: {disk_path}]"

    # Step 4: Inject meta footer
    if meta:
        meta_lines = []
        if "exit_code" in meta:
            meta_lines.append(f"Exit code: {meta['exit_code']}")
        if "url" in meta:
            meta_lines.append(f"URL: {meta['url']}")
        if "query" in meta:
            meta_lines.append(f"Query: {meta['query']}")
        if "duration" in meta:
            meta_lines.append(f"Duration: {meta['duration']:.2f}s")
        if meta_lines:
            truncated_text += f"\n\n{' | '.join(meta_lines)}"

    return truncated_text


# ── Backward compatibility ──
def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_call_id: str,
    max_chars: int = 20_000,
) -> str:
    """Legacy API — delegates to process_tool_result."""
    return process_tool_result(content, tool_name, tool_call_id)
