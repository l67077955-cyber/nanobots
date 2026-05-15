"""Centralized history, summarization, and context-pruning settings.

Loads ``~/.nanobot/history_settings.json`` once and exposes typed getters
so that shell.py, summarizer.py, tool_loop.py, context_pruning.py,
engine.py, broadcast.py, tool_chat.py, and the Telegram UI can all
read the same configuration without hardcoding defaults.

Settings are organized into four sections:

- **Top-level**: global limits (context window, tool result hard cap)
- **tool_results**: per-tool truncation + AI summarization
- **history**: conversation history window
- **context_pruning**: iterative context pruning thresholds
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

_SETTINGS_FILE = Path.home() / ".nanobot" / "history_settings.json"

# ── Defaults ──────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    # ── Top-level: global limits ──────────────────────────────────────
    "context_window_tokens": 200_000,
    "tool_result_max_chars": 64_000,

    # ── Stage 1 & 2: per-tool truncation + AI summarization ──────────
    "tool_results": {
        # Stage 1: per-tool raw output truncation (head+tail)
        "exec_max_chars": 10_000,
        "web_fetch_max_chars": 8_000,
        "web_search_max_chars": 5_000,

        # Stage 2: AI summarization
        "summarize_enabled": True,
        "summarize_threshold": 8_000,
        "summarize_model": "openai/gpt-4.1-nano",
        "summarize_max_input_chars": 8_000,
        "summarize_max_output_chars": 4_000,

        # Broadcast mode: tool_loop result_max_chars override
        "broadcast_result_max_chars": 20_000,
        # Direct mode: tool_loop result_max_chars override
        "direct_result_max_chars": 8_000,

        # HTML detection: when exec/web_fetch returns raw HTML,
        # inject a warning so the agent knows the result is unusable
        "html_detect_enabled": True,
    },

    # ── Stage 3: conversation history window ─────────────────────────
    "history": {
        "max_messages": 50,
        "max_context_chars": 100_000,
        # History compression: triggered at compress_ratio * max_messages
        "compress_ratio": 0.8,
        "compress_max_summary_tokens": 600,
        # Number of recent messages to keep in tail during compression
        "compression_keep_recent": 6,
        # Protect ALL user messages (not just the first) during compression
        "keep_user_messages": True,
        # AI summarization toggle for history compression (separate from tool_results)
        "history_summarize_enabled": True,
    },

    # ── Stage 4: iterative context pruning (tool_loop iteration 2+) ──
    "context_pruning": {
        "soft_ratio": 0.3,
        "hard_ratio": 0.5,
        "keep_recent": 3,
        "soft_max_chars": 4_000,
        "soft_head_chars": 1_500,
        "soft_tail_chars": 1_500,
    },
}

# Flat list of all known sections for merge logic
_SECTIONS = ("tool_results", "history", "context_pruning")
_TOP_LEVEL_KEYS = ("context_window_tokens", "tool_result_max_chars")

# ── Singleton cache ───────────────────────────────────────────────────────

_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    """Load settings, merging file on top of defaults."""
    global _cache
    if _cache is not None:
        return _cache

    result = json.loads(json.dumps(_DEFAULTS))  # deep copy
    if _SETTINGS_FILE.exists():
        try:
            user = json.loads(_SETTINGS_FILE.read_text())
            # Merge nested sections
            for section in _SECTIONS:
                if section in user and isinstance(user[section], dict):
                    result[section].update(user[section])
            # Merge top-level scalar fields
            for key in _TOP_LEVEL_KEYS:
                if key in user:
                    result[key] = user[key]
        except Exception as e:
            logger.warning("history_settings: failed to load {}: {}", _SETTINGS_FILE, e)

    _cache = result
    return result


def reload() -> dict[str, Any]:
    """Force reload from disk (e.g. after user edits via Telegram)."""
    global _cache
    _cache = None
    return _load()


def save(settings: dict[str, Any]) -> None:
    """Write settings to disk and update cache."""
    global _cache
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
    _cache = settings
    logger.info("history_settings: saved to {}", _SETTINGS_FILE)


def get_all() -> dict[str, Any]:
    """Return the full settings dict (read-only copy)."""
    return json.loads(json.dumps(_load()))


# ── Top-level getters ────────────────────────────────────────────────────

def get_context_window_tokens() -> int:
    return int(_load()["context_window_tokens"])


def get_tool_result_max_chars() -> int:
    return int(_load()["tool_result_max_chars"])


# ── tool_results getters ─────────────────────────────────────────────────

def exec_max_chars() -> int:
    return int(_load()["tool_results"]["exec_max_chars"])


def web_fetch_max_chars() -> int:
    return int(_load()["tool_results"]["web_fetch_max_chars"])


def web_search_max_chars() -> int:
    return int(_load()["tool_results"]["web_search_max_chars"])


def summarize_enabled() -> bool:
    return bool(_load()["tool_results"]["summarize_enabled"])


def summarize_threshold() -> int:
    return int(_load()["tool_results"]["summarize_threshold"])


def summarize_model() -> str:
    return str(_load()["tool_results"]["summarize_model"])


def summarize_max_input_chars() -> int:
    return int(_load()["tool_results"]["summarize_max_input_chars"])


def summarize_max_output_chars() -> int:
    return int(_load()["tool_results"]["summarize_max_output_chars"])


def broadcast_result_max_chars() -> int:
    return int(_load()["tool_results"]["broadcast_result_max_chars"])


def direct_result_max_chars() -> int:
    return int(_load()["tool_results"]["direct_result_max_chars"])


def html_detect_enabled() -> bool:
    return bool(_load()["tool_results"]["html_detect_enabled"])


# ── history getters ──────────────────────────────────────────────────────

def max_messages() -> int:
    return int(_load()["history"]["max_messages"])


def max_context_chars() -> int:
    return int(_load()["history"]["max_context_chars"])


def compress_ratio() -> float:
    return float(_load()["history"]["compress_ratio"])


def compress_max_summary_tokens() -> int:
    return int(_load()["history"]["compress_max_summary_tokens"])


def compression_keep_recent() -> int:
    return int(_load()["history"]["compression_keep_recent"])


def keep_user_messages() -> bool:
    return bool(_load()["history"]["keep_user_messages"])


def history_summarize_enabled() -> bool:
    return bool(_load()["history"]["history_summarize_enabled"])


# ── context_pruning getters ──────────────────────────────────────────────

def pruning_soft_ratio() -> float:
    return float(_load()["context_pruning"]["soft_ratio"])


def pruning_hard_ratio() -> float:
    return float(_load()["context_pruning"]["hard_ratio"])


def pruning_keep_recent() -> int:
    return int(_load()["context_pruning"]["keep_recent"])


def pruning_soft_max_chars() -> int:
    return int(_load()["context_pruning"]["soft_max_chars"])


def pruning_soft_head_chars() -> int:
    return int(_load()["context_pruning"]["soft_head_chars"])


def pruning_soft_tail_chars() -> int:
    return int(_load()["context_pruning"]["soft_tail_chars"])


# ── Field update (Telegram UI) ──────────────────────────────────────────

def update_field(section: str, key: str, value: Any) -> str:
    """Update a single field, save, and return confirmation.

    For top-level fields, pass section="__top__" and key as the field name.
    """
    settings = get_all()
    if section == "__top__":
        if key not in settings:
            return f"❌ 未知的配置项: {key}"
        old = settings[key]
        settings[key] = value
        save(settings)
        return f"✅ {key}: {old} → {value}"
    if section not in settings:
        return f"❌ 未知的配置区域: {section}"
    if not isinstance(settings[section], dict):
        return f"❌ {section} 不是可编辑区域"
    if key not in settings[section]:
        return f"❌ 未知的配置项: {section}.{key}"
    old = settings[section][key]
    settings[section][key] = value
    save(settings)
    return f"✅ {section}.{key}: {old} → {value}"
