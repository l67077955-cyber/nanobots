"""Small runtime helpers (request log, token meta).

Tool-call text for History commits: ``context.tool_log.build_tool_log``.
"""

from __future__ import annotations

from typing import Any

from nanobot.utils.helpers import cn_now as _cn_now


def reasoning_tokens_from_provider_meta(provider_meta: Any) -> int:
    """Sum reasoning token counts from provider_meta (dict or list of dicts)."""
    if isinstance(provider_meta, dict):
        return int(provider_meta.get("reasoning_tokens") or 0)
    if isinstance(provider_meta, list):
        return sum(
            int(m.get("reasoning_tokens") or 0)
            for m in provider_meta
            if isinstance(m, dict)
        )
    return 0


def log_request(
    engine: Any,
    agent: str,
    model: str,
    mode: str,
    reply_len: int = 0,
    **extra: Any,
) -> None:
    """Append a structured entry to engine._request_log."""
    entry: dict[str, Any] = {
        "agent": agent,
        "model": model,
        "reply_len": reply_len,
        "time": _cn_now().strftime("%H:%M:%S"),
        "mode": mode,
    }
    entry.update(extra)
    engine._request_log.append(entry)
    if len(engine._request_log) > 1000:
        engine._request_log = engine._request_log[-500:]