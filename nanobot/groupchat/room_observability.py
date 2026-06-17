"""Phase-0 room observability — structured events, zero behavior change.

Append-only audit log + in-memory ring buffer for future RoomState work.
Does not replace Telegram output; only records what already happened.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

_LOG_PATH = Path.home() / ".nanobot" / "logs" / "room_events.jsonl"
_RING_SIZE = 500
_lock = threading.Lock()
_ring: dict[str, deque[dict[str, Any]]] = {}


def observability_enabled() -> bool:
    return os.environ.get("NANOBOT_ROOM_OBS", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def resolve_room_id(channel: str | None = None, chat_id: str | None = None) -> str:
    if channel and chat_id:
        return f"{channel}:{chat_id}"
    if chat_id:
        return f"chat:{chat_id}"
    return "default"


def emit_room_event(
    *,
    room_id: str,
    kind: str,
    source: str = "engine",
    agent: str = "",
    content: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Record one structured event. Never raises; never blocks compute path."""
    if not observability_enabled():
        return

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "room_id": room_id,
        "kind": kind,
        "source": source,
    }
    if agent:
        record["agent"] = agent
    if content:
        record["content"] = _truncate(content)
    if extra:
        record["extra"] = extra

    try:
        with _lock:
            buf = _ring.setdefault(room_id, deque(maxlen=_RING_SIZE))
            buf.append(record)
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.debug("room_observability emit failed: {}", e)


def get_recent_events(room_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        buf = _ring.get(room_id)
        if not buf:
            return []
        return list(buf)[-limit:]


def _truncate(text: str, max_len: int = 400) -> str:
    text = text.replace("\r\n", "\n")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."