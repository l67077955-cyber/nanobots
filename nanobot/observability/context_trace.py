"""Context trace logging for agent LLM calls.

The request log records the final provider payload.  This module records the
semantic context snapshot selected by tool_loop before provider-specific
rewrites such as cache markers, sanitizing, or tool flattening.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger


def _trace_root() -> Path:
    return Path.home() / ".nanobot" / "context_traces"


def canonical_json(value: Any) -> str:
    """Return stable JSON suitable for hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_messages(messages: list[dict[str, Any]]) -> str:
    return stable_hash(messages)


def _content_len(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block.get("text", ""))
            for block in content
            if isinstance(block, dict)
        )
    return 0


def _message_hashes(messages: list[dict[str, Any]]) -> list[str]:
    return [stable_hash(message) for message in messages]


def write_context_snapshot(
    *,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    model: str,
    iteration: int,
    parent_context_id: str | None = None,
    pruned_from_count: int | None = None,
    tools_count: int | None = None,
) -> dict[str, Any]:
    """Persist a context snapshot and return trace metadata.

    Logging failures are swallowed so observability never blocks an LLM call.
    """
    meta = metadata or {}
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    context_id = f"ctx_{hash_messages(messages)[:24]}"
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    total_chars = sum(_content_len(message.get("content")) for message in messages)

    root = _trace_root()
    blob_dir = root / "blobs"
    event_dir = root / "events"
    blob_path = blob_dir / f"{context_id}.json.gz"
    event_path = event_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

    event: dict[str, Any] = {
        "type": "context_snapshot",
        "ts": now,
        "request_id": request_id,
        "context_id": context_id,
        "parent_context_id": parent_context_id,
        "agent": meta.get("log_agent"),
        "session": meta.get("log_session"),
        "topic": meta.get("log_topic"),
        "mode": meta.get("log_mode"),
        "model": model,
        "iteration": iteration,
        "messages_count": len(messages),
        "total_chars": total_chars,
        "pruned_from_messages_count": pruned_from_count,
        "tools_count": tools_count,
        "message_hashes": _message_hashes(messages),
        "snapshot_path": str(blob_path),
    }

    try:
        blob_dir.mkdir(parents=True, exist_ok=True)
        event_dir.mkdir(parents=True, exist_ok=True)

        if not blob_path.exists():
            snapshot = {
                "type": "context_snapshot_blob",
                "ts": now,
                "context_id": context_id,
                "metadata": {
                    "agent": meta.get("log_agent"),
                    "session": meta.get("log_session"),
                    "topic": meta.get("log_topic"),
                    "mode": meta.get("log_mode"),
                    "model": model,
                    "iteration": iteration,
                    "parent_context_id": parent_context_id,
                },
                "messages": messages,
            }
            with gzip.open(blob_path, "wt", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, default=str)

        with open(event_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("Context trace logging failed: {}", exc)

    return {
        "request_id": request_id,
        "context_id": context_id,
        "parent_context_id": parent_context_id,
        "iteration": iteration,
        "snapshot_path": str(blob_path),
    }
