"""Unified tool result post-processing pipeline.

Single chokepoint for ALL tool results after execution.
Replaces scattered truncation in shell.py, web.py, and tool_storage.py.

Pipeline: normalize → truncate → persist → inject_meta
"""

from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Any
from loguru import logger

# Prefer a user-persistent location (survives container/tmp cleans).
# Old /tmp location was ephemeral and caused lost full results after restarts.
STORAGE_DIR = Path.home() / ".nanobot" / "tool_results"

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



async def _maybe_ai_summarize(text: str, tool_name: str) -> str:
    """If AI summarization is enabled and text exceeds threshold, summarize via cheap LLM."""
    try:
        from nanobot.groupchat.history import history_settings as hs
        settings = hs.get_all()
        tr = settings.get("tool_results", {})
        if not tr.get("summarize_enabled", True):
            return text
        threshold = int(tr.get("summarize_threshold", 8000))
        if len(text) <= threshold:
            return text
        model = tr.get("summarize_model", "openai/gpt-4.1-nano")
        max_input = int(tr.get("summarize_max_input_chars", 8000))
        max_output = int(tr.get("summarize_max_output_chars", 4000))

        from nanobot.providers.litellm_provider import LiteLLMProvider
        import json as _json

        api_key, api_base, provider_name = "", "", "openrouter"
        try:
            cfg_path = Path.home() / ".nanobot" / "config.json"
            if cfg_path.exists():
                cfg = _json.loads(cfg_path.read_text())
                pcfg = (cfg.get("providers") or {}).get(provider_name, {}) or {}
                api_key = pcfg.get("apiKey", "")
                api_base = pcfg.get("apiBase", "")
        except Exception:
            pass

        llm = LiteLLMProvider(
            default_model=model,
            api_key=api_key or None,
            api_base=api_base or None,
            provider_name=provider_name,
        )

        prompt = (
            "You are a tool result summarizer. Extract the most relevant info, "
            "preserve numbers, dates, URLs. Output in the same language as the source. "
            "Only extract existing info, never fabricate.\n\n"
            f"--- Tool result ({len(text)} chars, tool={tool_name}) ---\n"
            f"{text[:max_input]}\n--- End ---"
        )

        logger.info("result_processor: AI summarizing {}c via {} (tool={})",
                     len(text), model, tool_name)
        response = await asyncio.wait_for(
            llm.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=max_output,
                temperature=0.1,
            ),
            timeout=30.0,
        )
        summary = response.content.strip() if response.content else None
        if summary and len(summary) < len(text):
            usage = response.usage or {}
            tok_info = f" | nano in:{usage.get('prompt_tokens',0)} out:{usage.get('completion_tokens',0)}"
            logger.info("result_processor: {}c -> {}c -{}% (tool={}{})",
                         len(text), len(summary),
                         round((len(text)-len(summary))/len(text)*100),
                         tool_name, tok_info)
            return f"{summary}\n\n`[nano:{tool_name}] {len(text)}->{len(summary)}c`"
        return text
    except Exception as e:
        logger.warning("result_processor: AI summarize failed for {}: {}", tool_name, e)
        return text



async def process_tool_result(
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

    # Step 1.5: AI summarize (if enabled and over threshold)
    text = await _maybe_ai_summarize(text, tool_name)

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
async def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_call_id: str,
    max_chars: int = 20_000,
) -> str:
    """Legacy API — delegates to process_tool_result."""
    return await process_tool_result(content, tool_name, tool_call_id)
