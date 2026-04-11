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
    """Create a full snapshot of messages for logging (before tool_loop mutates them)."""
    snap: list[dict[str, Any]] = []
    for m in messages:
        entry: dict[str, Any] = {"role": m.get("role", "?")}
        if m.get("name"):
            entry["name"] = m["name"]
        content = m.get("content", "")
        if isinstance(content, str):
            entry["content"] = content
            entry["content_len"] = len(content)
        elif isinstance(content, list):
            text_parts = [b.get("text", "") for b in content if isinstance(b, dict)]
            joined = " ".join(text_parts)
            entry["content"] = joined
            entry["content_len"] = len(joined)
        else:
            entry["content"] = str(content) if content else ""
            entry["content_len"] = len(str(content)) if content else 0
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
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
        "cost": result.cost,
        "cache_tokens": result.cache_tokens,
        "provider_meta": result.provider_meta,
    }


# ── Tool callback factory ───────────────────────────────────

def make_tool_callbacks(
    agent_name: str,
    save_event: Callable,
    send_fn: Callable[[str], Awaitable[None]] | None,
    send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None,
    edit_fn: Callable[[int, str], Awaitable[None]] | None,
    iter_usage_ref: dict | None = None,
) -> tuple[Callable, Callable]:
    """Create on_tool_start / on_tool_result callbacks for an agent.

    Args:
        iter_usage_ref: Shared mutable dict updated with per-iteration token
            usage before tool execution. When provided, a token suffix is
            appended to the tool result message.

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
            "args": dict(args),
        })
        # Full logging to server log
        import json as _json_tc
        logger.info(
            "tool_chat [{}] tool_call: {}({})",
            agent_name, name, _json_tc.dumps(args, ensure_ascii=False),
        )
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
        # Full result logging to server log
        logger.info(
            "tool_chat [{}] tool_result: {} ({}c): {}",
            agent_name, name, len(result) if result else 0, result,
        )
        if not result:
            _tool_msg_id = None
            return
        rlen = len(result)
        preview = result.strip().replace("\n", " ")[:60]
        result_line = f"↳ {preview}{'…' if rlen > 60 else ''} ({rlen:,}字)"

        # Build token suffix from per-iteration usage if available
        token_suffix = ""
        if iter_usage_ref:
            u = iter_usage_ref
            p = u.get("prompt_tokens", 0)
            c = u.get("completion_tokens", 0)
            total = u.get("total_tokens", 0) or (p + c)
            cost = u.get("cost")
            cache_t = u.get("cache_tokens", 0) or u.get("cache_read_input_tokens", 0)
            if total:
                token_suffix = "\n" + _d.format_token_stats(p, c, cost=cost, cache_tokens=cache_t)

        if _tool_msg_id and edit_fn and _tool_msg_text:
            try:
                updated = f"{_tool_msg_text}\n{result_line}{token_suffix}"
                await edit_fn(_tool_msg_id, updated)
            except Exception:
                if send_fn:
                    await send_fn(result_line + token_suffix)
        elif send_fn:
            await send_fn(result_line + token_suffix)
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
    # Shared mutable dict updated with per-iteration token usage so that
    # on_tool_result can append a token suffix to the tool call message.
    _iter_usage_ref: dict = {}
    default_start, default_result = make_tool_callbacks(
        agent_name, _save_event, send_fn, send_and_get_id_fn, edit_fn,
        iter_usage_ref=_iter_usage_ref,
    )

    # Load configurable result_max_chars for direct mode
    try:
        from nanobot.groupchat.history_settings import direct_result_max_chars
        _direct_result_max = direct_result_max_chars()
    except Exception:
        _direct_result_max = 8_000

    effective_defs = None if force_no_tools else (tool_defs if tool_defs else None)
    # Compute context stats for logging
    _total_chars = sum(
        len(m.get("content", "")) if isinstance(m.get("content"), str)
        else sum(len(b.get("text", "")) for b in m.get("content", []) if isinstance(b, dict))
        if isinstance(m.get("content"), list) else 0
        for m in messages
    )
    logger.info(
        "chat_with_tools: agent={} model={} tool_defs={} is_direct={} msgs={} total_chars={}",
        agent_name, model, len(tool_defs) if tool_defs else 0, is_direct,
        len(messages), _total_chars,
    )

    # Snapshot messages before tool_loop mutates them
    messages_snap = snapshot_messages(messages)
    sampling = dict(getattr(provider, "sampling_params", {}))
    tool_names = [d.get("function", {}).get("name", "?") for d in (tool_defs or [])]

    # Per-iteration token usage callback — update shared ref so tool callbacks
    # can show a token suffix on each tool call message.
    async def _on_iter_usage(usage: dict) -> None:
        _iter_usage_ref.clear()
        _iter_usage_ref.update(usage)

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
        on_iteration_usage=_on_iter_usage,
        on_content_delta=on_content_delta,
        on_content_reset=on_content_reset,
        clean_response=clean_response,
        result_max_chars=_direct_result_max,
    )

    content = result.content or ""
    stats = build_stats(result, tool_defs, tool_names, messages_snap, sampling, max_tokens)

    # Log complete result
    logger.info(
        "chat_with_tools result: agent={} iters={} latency={:.2f}s "
        "tokens={} tools_used={} finish={} content={}",
        agent_name, result.iterations, result.latency,
        result.token_usage, result.tools_used, result.finish_reason, content,
    )

    return content, result.tools_used, stats
