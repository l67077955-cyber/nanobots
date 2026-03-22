"""Shared utilities for the groupchat package."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

CST = timezone(timedelta(hours=8))


def cn_now() -> datetime:
    """Return current time in China Standard Time (UTC+8)."""
    return datetime.now(CST)


def build_tool_log(tool_calls_detail: list[dict[str, Any]]) -> str:
    """Build a tool call summary for conversation history.

    Appended to the assistant's content so the model can see what tools
    it previously called on the next turn.  Preview lengths vary by tool
    type — search/fetch results get longer previews (the model needs to
    remember *what* it found), while exec/chatroom keep it shorter.

    Total output is capped at ~4000 chars to prevent context bloat.

    Returns empty string if no tool calls were made.
    """
    if not tool_calls_detail:
        return ""

    # Per-tool preview length limits
    _PREVIEW_LIMITS = {
        "web_search": 1500,
        "web_fetch": 1500,
        "read_file": 800,
        "exec": 500,
        "list_dir": 300,
        "chatroom_send": 200,
        "wait": 200,
        "write_file": 100,
        "edit_file": 100,
    }
    _DEFAULT_PREVIEW = 500
    _TOTAL_CAP = 4000

    lines: list[str] = []
    total_chars = 0

    for tc in tool_calls_detail:
        name = tc.get("name", "?")
        args_raw = tc.get("args", "")
        result_len = tc.get("result_len", 0)
        preview = tc.get("result_preview", "")
        success = tc.get("success", True)
        is_dup = tc.get("duplicate", False)

        # Extract the key argument (query / url / command / path) for display
        try:
            args_dict = __import__("json").loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args_dict = {}
        key_arg = (
            args_dict.get("query")
            or args_dict.get("command")
            or args_dict.get("url")
            or args_dict.get("path")
            or args_dict.get("message", "")[:80]
            or ""
        )
        if isinstance(key_arg, str) and len(key_arg) > 80:
            key_arg = key_arg[:77] + "..."

        # Build result info with tool-appropriate preview length
        max_preview = _PREVIEW_LIMITS.get(name, _DEFAULT_PREVIEW)

        if is_dup:
            result_info = "[重复调用,已跳过]"
        elif not success:
            err = tc.get("error", "")
            result_info = f"[失败: {err[:80]}]"
        elif name in ("chatroom_send", "wait", "write_file", "edit_file"):
            # For communication/write tools, just show status
            result_info = f"({result_len:,}字)" if result_len else "OK"
        elif isinstance(preview, str) and preview:
            # Remaining budget check
            remaining = _TOTAL_CAP - total_chars
            effective_limit = min(max_preview, remaining)
            if effective_limit < 50:
                result_info = f"({result_len:,}字)"
            else:
                short = preview.strip()[:effective_limit]
                truncated = len(preview) > effective_limit
                result_info = f"{short}{'…' if truncated else ''} ({result_len:,}字)"
        else:
            result_info = f"({result_len:,}字)"

        line = f"• {name}({key_arg}) → {result_info}"
        lines.append(line)
        total_chars += len(line)

        # Hard cap: stop adding more details
        if total_chars >= _TOTAL_CAP:
            remaining_count = len(tool_calls_detail) - len(lines)
            if remaining_count > 0:
                lines.append(f"  (还有 {remaining_count} 个工具调用，已省略)")
            break

    if not lines:
        return ""

    return "\n\n[工具调用记录]\n" + "\n".join(lines)


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

