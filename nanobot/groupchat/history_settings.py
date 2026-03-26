"""Centralized history and tool-result settings.

Loads ``~/.nanobot/history_settings.json`` once and exposes typed getters
so that shell.py, summarizer.py, tool_loop.py, and engine.py can all
read the same configuration without hardcoding defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

_SETTINGS_FILE = Path.home() / ".nanobot" / "history_settings.json"

# ── Defaults ──────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    "context_window_tokens": 200_000,
    "tool_result_max_chars": 64_000,
    "tool_results": {
        "exec_max_chars": 10_000,
        "web_fetch_max_chars": 8_000,
        "web_search_max_chars": 5_000,
        "summarize_threshold": 8_000,
        "summarize_model": "openai/gpt-4.1-nano",
        "summarize_enabled": True,
    },
    "history": {
        "max_messages": 50,
        "max_context_chars": 100_000,
    },
}

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
            for section in ("tool_results", "history"):
                if section in user and isinstance(user[section], dict):
                    result[section].update(user[section])
            # Merge top-level scalar fields
            for key in ("context_window_tokens", "tool_result_max_chars"):
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


# ── Typed getters ─────────────────────────────────────────────────────────

def exec_max_chars() -> int:
    return int(_load()["tool_results"]["exec_max_chars"])


def web_fetch_max_chars() -> int:
    return int(_load()["tool_results"]["web_fetch_max_chars"])


def web_search_max_chars() -> int:
    return int(_load()["tool_results"]["web_search_max_chars"])


def summarize_threshold() -> int:
    return int(_load()["tool_results"]["summarize_threshold"])


def summarize_model() -> str:
    return str(_load()["tool_results"]["summarize_model"])


def summarize_enabled() -> bool:
    return bool(_load()["tool_results"]["summarize_enabled"])


def max_messages() -> int:
    return int(_load()["history"]["max_messages"])


def max_context_chars() -> int:
    return int(_load()["history"]["max_context_chars"])


def get_tool_result_max_chars() -> int:
    return int(_load()["tool_result_max_chars"])


def get_context_window_tokens() -> int:
    return int(_load()["context_window_tokens"])


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
