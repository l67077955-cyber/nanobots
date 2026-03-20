"""Tool-augmented chat for group chat agents.

Extracts the tool calling loop from ``engine.py``, including:
- Tool callback factories (on_tool_start / on_tool_result display)
- Message snapshot for structured logging
- Stats packaging from tool_loop result
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat import display as _d


# ── Helpers ──────────────────────────────────────────────────

def snapshot_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a lightweight snapshot of messages for logging (before tool_loop mutates them)."""
    snap: list[dict[str, Any]] = []
    for m in messages:
        entry: dict[str, Any] = {"role": m.get("role", "?")}
        if m.get("name"):
            entry["name"] = m["name"]
        content = m.get("content", "")
        if isinstance(content, str):
            entry["content"] = content[:500]
            entry["content_len"] = len(content)
        elif isinstance(content, list):
            text_parts = [b.get("text", "") for b in content if isinstance(b, dict)]
            joined = " ".join(text_parts)
            entry["content"] = joined[:500]
            entry["content_len"] = len(joined)
        else:
            entry["content"] = str(content)[:500] if content else ""
            entry["content_len"] = len(str(content)) if content else 0
        snap.append(entry)
    return snap


def build_stats(result: Any, tool_defs: list | None, tool_names: list[str],
                messages_snapshot: list[dict], sampling: dict, max_tokens: int) -> dict[str, Any]:
    """Package tool_loop result into a stats dict."""
    return {
        "iterations": result.iterations,
        "latency": result.latency,
        "tokens": result.token_usage,
        "calls": result.call_details,
        "tool_calls_detail": result.tool_calls_detail,
        "tools_available": result.tools_available,
        "tool_defs_count": len(tool_defs) if tool_defs else 0,
        "tool_names": tool_names,
        "messages_snapshot": messages_snapshot,
        "sampling_params": sampling,
        "max_tokens": max_tokens,
        "status_code": result.status_code,
        "finish_reason": result.finish_reason,
    }


# ── Tool callback factory ───────────────────────────────────

def make_tool_callbacks(
    agent_name: str,
    save_event: Callable,
    send_fn: Callable[[str], Awaitable[None]] | None,
    send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None,
    edit_fn: Callable[[int, str], Awaitable[None]] | None,
) -> tuple[Callable, Callable]:
    """Create on_tool_start / on_tool_result callbacks for an agent.

    Returns:
        (on_tool_start, on_tool_result) async callbacks.
    """
    _tool_msg_id: int | None = None
    _tool_msg_text: str = ""

    async def on_tool_start(name: str, args: dict) -> None:
        nonlocal _tool_msg_id, _tool_msg_text
        _tool_msg_id = None
        _tool_msg_text = ""
        if not isinstance(args, dict):
            args = {}
        # Persist tool_call event
        save_event("tool_call", agent=agent_name, extra={
            "tool": name,
            "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in args.items()},
        })
        short = (
            args.get("command") or args.get("query")
            or args.get("url") or args.get("path") or ""
        )
        if not short and args:
            short = list(args.values())[0]
        if isinstance(short, str) and len(short) > 80:
            short = short[:80] + "…"
        text = _d.tool_call_line(agent_name, name, short if isinstance(short, str) else str(short))
        _tool_msg_text = text
        if send_and_get_id_fn:
            _tool_msg_id = await send_and_get_id_fn(text)
        elif send_fn:
            await send_fn(text)

    async def on_tool_result(name: str, tool_call_id: str, result: str) -> None:
        nonlocal _tool_msg_id, _tool_msg_text
        save_event("tool_result", agent=agent_name, extra={
            "tool": name,
            "result_len": len(result) if result else 0,
            "success": not (result or "").startswith("Error:"),
        })
        if not result:
            _tool_msg_id = None
            return
        rlen = len(result)
        preview = result.strip().replace("\n", " ")[:80]
        result_line = f"↳ {preview}{'…' if rlen > 80 else ''} ({rlen}字)"
        if _tool_msg_id and edit_fn and _tool_msg_text:
            try:
                updated = f"{_tool_msg_text}\n{result_line}"
                await edit_fn(_tool_msg_id, updated)
            except Exception:
                if send_fn:
                    await send_fn(f"   {result_line}")
        elif send_fn:
            await send_fn(f"   {result_line}")
        _tool_msg_id = None
        _tool_msg_text = ""

    return on_tool_start, on_tool_result


# ── Main function ────────────────────────────────────────────

async def chat_with_tools(
    *,
    provider: Any,
    messages: list[dict[str, Any]],
    model: str,
    agent_name: str,
    tool_registry: Any,
    tool_defs: list | None,
    max_tokens: int,
    max_iterations: int = 5,
    session_id: str = "direct",
    is_direct: bool = False,
    debug_context: bool = False,
    topic: str = "",
    clean_response: Callable[[str], str] | None = None,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_content_reset: Callable[[], Awaitable[None]] | None = None,
    on_tool_start_override: Callable | None = None,
    on_tool_result_override: Callable | None = None,
    save_event: Callable | None = None,
    send_fn: Callable[[str], Awaitable[None]] | None = None,
    send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None = None,
    edit_fn: Callable[[int, str], Awaitable[None]] | None = None,
    force_no_tools: bool = False,
) -> tuple[str, list[str], dict[str, Any]]:
    """Run tool-augmented chat loop. Standalone version of engine._chat_with_tools.

    Returns:
        (content, tools_used, stats)
    """
    from nanobot.agent.tool_loop import tool_loop

    # Langfuse trace metadata
    trace_metadata = {
        "trace_name": f"{'direct' if is_direct else 'group'}_{agent_name}",
        "trace_user_id": "groupchat",
        "tags": [agent_name, "direct" if is_direct else "group"],
        "generation_name": f"{agent_name}_loop",
        "debug_context": debug_context,
        "log_agent": agent_name,
        "log_session": session_id,
        "log_topic": topic,
        "log_mode": "direct" if is_direct else "group",
    }

    # Default tool callbacks
    _save_event = save_event or (lambda *a, **kw: None)
    default_start, default_result = make_tool_callbacks(
        agent_name, _save_event, send_fn, send_and_get_id_fn, edit_fn,
    )

    effective_defs = None if force_no_tools else (tool_defs if tool_defs else None)
    logger.info(
        "chat_with_tools: agent={} model={} tool_defs={} is_direct={}",
        agent_name, model, len(tool_defs) if tool_defs else 0, is_direct,
    )

    # Snapshot messages before tool_loop mutates them
    messages_snap = snapshot_messages(messages)
    sampling = dict(getattr(provider, "sampling_params", {}))
    tool_names = [d.get("function", {}).get("name", "?") for d in (tool_defs or [])]

    result = await tool_loop(
        provider=provider,
        messages=messages,
        tool_registry=tool_registry,
        model=model,
        max_tokens=max_tokens,
        max_iterations=max_iterations,
        tool_defs=effective_defs,
        metadata=trace_metadata,
        on_tool_start=on_tool_start_override or default_start,
        on_tool_result=on_tool_result_override or default_result,
        on_content_delta=on_content_delta,
        on_content_reset=on_content_reset,
        clean_response=clean_response,
        result_max_chars=20_000,
    )

    content = result.content or ""
    stats = build_stats(result, tool_defs, tool_names, messages_snap, sampling, max_tokens)
    return content, result.tools_used, stats
