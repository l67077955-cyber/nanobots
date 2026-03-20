"""Shared utilities for the groupchat package."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

CST = timezone(timedelta(hours=8))


def cn_now() -> datetime:
    """Return current time in China Standard Time (UTC+8)."""
    return datetime.now(CST)


def log_request(
    engine: Any,
    agent: str,
    model: str,
    mode: str,
    reply_len: int = 0,
    **extra: Any,
) -> None:
    """Append a structured entry to engine._request_log.

    Centralizes the common request logging pattern used by speaker,
    direct_chat, broadcast, and orchestra modules.
    """
    entry: dict[str, Any] = {
        "agent": agent,
        "model": model,
        "reply_len": reply_len,
        "time": cn_now().strftime("%H:%M:%S"),
        "mode": mode,
    }
    entry.update(extra)
    engine._request_log.append(entry)

