"""Lightweight tool-output summarizer using a cheap reader model.

When a tool (e.g. ``exec``) produces output larger than a threshold,
this module compresses it via a small LLM (default: gpt-4.1-nano)
before the result is injected back into the conversation context.

Falls back to head+tail truncation on any failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

# ── Defaults ──────────────────────────────────────────────────────────────

_DEFAULT_READER_MODEL = "openai/gpt-4.1-nano"
_DEFAULT_PROVIDER = "openrouter"


def _load_reader_config() -> dict[str, Any]:
    """Load reader agent config from ``~/.nanobot/agents/reader/config.json``."""
    cfg_path = Path.home() / ".nanobot" / "agents" / "reader" / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            model = cfg.get("model")
            provider = cfg.get("provider")
            result: dict[str, Any] = {}
            if model:
                result["model"] = model
            if provider:
                result["provider"] = provider
            return result
        except Exception:
            pass
    return {}


def _head_tail_truncate(text: str, max_chars: int) -> str:
    """Fallback: keep first half + last half with a truncation marker."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n... ({len(text) - max_chars:,} chars truncated) ...\n\n"
        + text[-half:]
    )


async def summarize_tool_output(
    tool_name: str,
    raw_output: str,
    *,
    threshold: int | None = None,
    max_input_chars: int | None = None,
    max_output_chars: int | None = None,
) -> tuple[str, bool]:
    """Summarize a tool's output if it exceeds *threshold* characters.

    Returns ``(processed_text, was_summarized)``.

    - If ``len(raw_output) <= threshold``: returns ``(raw_output, False)``.
    - If summarization succeeds: returns ``(summary + stats_tag, True)``.
    - On failure: falls back to head+tail truncation.
    """
    # Load settings dynamically
    try:
        from nanobot.groupchat.history import history_settings as hs
        if threshold is None:
            threshold = hs.summarize_threshold()
        if max_input_chars is None:
            max_input_chars = hs.summarize_max_input_chars()
        if max_output_chars is None:
            max_output_chars = hs.summarize_max_output_chars()
        if not hs.summarize_enabled():
            # Summarization disabled — just truncate
            return _head_tail_truncate(raw_output, threshold), False
    except Exception:
        if threshold is None:
            threshold = 8000
        if max_input_chars is None:
            max_input_chars = 8000
        if max_output_chars is None:
            max_output_chars = 4000

    if len(raw_output) <= threshold:
        return raw_output, False

    try:
        summary, usage = await _call_reader(tool_name, raw_output, max_input_chars)
        if summary:
            saved = len(raw_output) - len(summary)
            pct = round(saved / len(raw_output) * 100)
            nano_t = usage.get("total_tokens", 0)
            nano_p = usage.get("prompt_tokens", 0)
            nano_c = usage.get("completion_tokens", 0)
            tok_info = f" | nano in:{nano_p} out:{nano_c} Σ{nano_t}" if nano_t else ""
            tag = f"`[nano:{tool_name}] {len(raw_output)}→{len(summary)}c -{pct}%{tok_info}`"
            result = f"{summary}\n\n{tag}"
            # Ensure we didn't accidentally make it longer
            if len(result) < len(raw_output):
                logger.info(
                    "summarize_tool_output: {}() {}c → {}c (-{}%)",
                    tool_name, len(raw_output), len(result), pct,
                )
                return result, True
            logger.warning(
                "summarize_tool_output: summary was longer than original, using truncation"
            )
    except Exception as e:
        logger.warning("summarize_tool_output: LLM failed for {}(): {}", tool_name, e)

    # Fallback: head+tail truncation
    return _head_tail_truncate(raw_output, threshold), False


async def _call_reader(
    tool_name: str,
    raw_output: str,
    max_input_chars: int,
) -> tuple[str | None, dict]:
    """Call the reader model to summarize tool output."""
    from nanobot.providers.litellm_provider import LiteLLMProvider

    reader_cfg = _load_reader_config()
    # Use history_settings model if available, then reader config, then default
    try:
        from nanobot.groupchat.history import history_settings as hs
        settings_model = hs.summarize_model()
    except Exception:
        settings_model = None
    model = settings_model or reader_cfg.get("model", _DEFAULT_READER_MODEL)
    provider_name = reader_cfg.get("provider", _DEFAULT_PROVIDER)

    # Load provider credentials
    api_key, api_base = "", ""
    try:
        cfg_path = Path.home() / ".nanobot" / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
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

    # Truncate input to the reader to avoid excessive cost
    input_text = raw_output[:max_input_chars]

    prompt = (
        f"你是一个工具输出摘要助手。以下是 `{tool_name}` 工具的执行输出。\n"
        "请将其精炼为简洁的摘要，保留所有关键信息：\n"
        "- 命令的成功/失败状态和退出码\n"
        "- 关键数据、数字、路径、版本号\n"
        "- 错误信息（如有）\n"
        "- 重要输出的前几行和最后几行\n"
        "去掉重复、冗余的中间输出。用原文语言输出。\n\n"
        f"--- 工具输出 ({len(raw_output)} chars) ---\n"
        f"{input_text}\n"
        "--- 结束 ---"
    )

    logger.info(
        "summarize_tool_output: calling {}/{} for {}() (input={}c)",
        provider_name, model, tool_name, len(input_text),
    )

    response = await llm.chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_output_chars,
        temperature=0.1,
    )
    usage = response.usage or {}
    result = response.content.strip() if response.content else None
    return result, usage
