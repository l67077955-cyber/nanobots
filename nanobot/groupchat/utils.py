"""Shared utilities for the groupchat package."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


def cn_now() -> datetime:
    """Return current time in China Standard Time (UTC+8)."""
    return datetime.now(CST)
