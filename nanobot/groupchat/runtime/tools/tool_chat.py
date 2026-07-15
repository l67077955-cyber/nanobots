"""Tool-augmented chat for group chat agents.

Provides chat_with_tools() and helpers used by GroupChatEngine._chat_with_tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.config.validate import SAMPLING_KEYS


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


def valid_agent_sampling(agent_sampling: dict[str, Any] | None) -> dict[str, Any]:
    """Filter per-agent sampling params to keys providers know how to send."""
    if not isinstance(agent_sampling, dict):
        return {}
    return {
        key: value
        for key, value in agent_sampling.items()
        if key in SAMPLING_KEYS or key.startswith("reasoning")
    }


def resolve_max_tool_iterations(engine: Any, agent_name: str, *, is_direct: bool = False) -> int:
    """Resolve per-agent tool iteration budget for direct chat / tool loops."""
    default = 12 if is_direct else 8
    agent_cfg = engine.registry.get(agent_name, {}) if hasattr(engine, "registry") else {}

    for key in ("max_tool_iterations", "maxToolIterations"):
        val = agent_cfg.get(key)
        if isinstance(val, int) and val > 0:
            return val

    agent_dir = agent_cfg.get("agent_dir")
    if agent_dir:
        config_file = Path(agent_dir) / "config.json"
        if config_file.exists():
            try:
                import json as _json
                raw = _json.loads(config_file.read_text())
                for key in ("max_tool_iterations", "maxToolIterations"):
                    val = raw.get(key)
                    if isinstance(val, int) and val > 0:
                        return val
                defaults = (raw.get("agents") or {}).get("defaults") or {}
                for key in ("max_tool_iterations", "maxToolIterations"):
                    val = defaults.get(key)
                    if isinstance(val, int) and val > 0:
                        return val
            except Exception:
                pass

    try:
        from nanobot.config.loader import load_config
        global_defaults = load_config().agents.defaults
        if global_defaults.max_tool_iterations > 0:
            return global_defaults.max_tool_iterations
    except Exception:
        pass

    return default


def build_stats(
    result: Any,
    tool_defs: list | None,
    tool_names: list[str],
    messages_snapshot: list[dict],
    sampling: dict,
    max_tokens: int,
) -> dict[str, Any]:
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


def make_tool_callbacks(
    agent_name: str,
    save_event: Callable,
    send_fn: Callable[[str], Awaitable[None]] | None,
    send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None,
    edit_fn: Callable[[int, str], Awaitable[None]] | None,
    iter_usage_ref: dict | None = None,
) -> tuple[Callable, Callable]:
    """Create on_tool_start / on_tool_result callbacks for an agent."""
    _pending_tools: dict[str, tuple[int | None, str]] = {}
    _temp_counter = 0

    async def on_tool_start(
        name: str,
        args: dict,
        tool_call_id: str | None = None,
    ) -> None:
        nonlocal _temp_counter
        if not isinstance(args, dict):
            args = {}
        save_event("tool_call", agent=agent_name, extra={
            "tool": name,
            "args": dict(args),
        })
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

        if tool_call_id is None:
            _temp_counter += 1
            tool_call_id = f"_temp-{agent_name}-{_temp_counter}"

        msg_id: int | None = None
        if send_and_get_id_fn:
            msg_id = await send_and_get_id_fn(text)
        elif send_fn:
            await send_fn(text)

        _pending_tools[tool_call_id] = (msg_id, text)

    async def on_tool_result(name: str, tool_call_id: str, result: str) -> None:
        result_str = str(result or "")
        save_event("tool_result", agent=agent_name, extra={
            "tool": name,
            "result_len": len(result_str),
            "success": not result_str.startswith("Error:"),
        })
        logger.info(
            "tool_chat [{}] tool_result: {} ({}c): {}",
            agent_name, name, len(result_str), result_str,
        )
        if not result_str:
            _pending_tools.pop(tool_call_id, None)
            return
        rlen = len(result_str)
        preview = result_str.strip().replace("\n", " ")[:60]
        result_line = f"↳ {preview}{'…' if rlen > 60 else ''} ({rlen:,}字)"

        token_suffix = ""
        if iter_usage_ref:
            u = iter_usage_ref
            p = u.get("prompt", u.get("prompt_tokens", 0))
            c = u.get("completion", u.get("completion_tokens", 0))
            total = u.get("total", u.get("total_tokens", 0)) or (p + c)
            cost = u.get("cost")
            cache_t = u.get("cache_tokens", 0) or u.get("cache_read_input_tokens", 0)
            if total:
                token_suffix = "\n" + _d.format_token_stats(p, c, cost=cost, cache_tokens=cache_t)

        pending = _pending_tools.pop(tool_call_id, None)
        if pending and pending[0] is not None and edit_fn and pending[1]:
            try:
                updated = f"{pending[1]}\n{result_line}{token_suffix}"
                await edit_fn(pending[0], updated)
            except Exception as exc:
                logger.warning("tool_chat [{}] edit_fn failed: {}", agent_name, exc)
                if send_fn:
                    await send_fn(result_line + token_suffix)
        elif send_fn:
            await send_fn(result_line + token_suffix)

    return on_tool_start, on_tool_result


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
    agent_sampling: dict[str, Any] | None = None,
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
    """Run tool-augmented chat loop."""
    from nanobot.groupchat.runtime.tools.tool_loop import tool_loop

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

    _save_event = save_event or (lambda *a, **kw: None)
    _iter_usage_ref: dict = {}
    default_start, default_result = make_tool_callbacks(
        agent_name, _save_event, send_fn, send_and_get_id_fn, edit_fn,
        iter_usage_ref=_iter_usage_ref,
    )

    try:
        from nanobot.groupchat.context.history_settings import direct_result_max_chars
        _direct_result_max = direct_result_max_chars()
    except Exception:
        _direct_result_max = 8_000

    effective_defs = None if force_no_tools else tool_defs

    def _calc_total_chars(msgs: list[dict]) -> int:
        total = 0
        for m in msgs:
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(block.get("text", ""))
        return total

    _total_chars = _calc_total_chars(messages)
    logger.info(
        "chat_with_tools: agent={} model={} tool_defs={} is_direct={} msgs={} total_chars={}",
        agent_name, model, len(tool_defs) if tool_defs else 0, is_direct,
        len(messages), _total_chars,
    )

    messages_snap = snapshot_messages(messages)

    base = getattr(provider, "sampling_params", {}) or {}
    sampling = dict(base)
    if agent_sampling:
        clean_agent_sampling = valid_agent_sampling(agent_sampling)
        sampling.update(clean_agent_sampling)
        ignored_keys = sorted(set(agent_sampling) - set(clean_agent_sampling))
        if ignored_keys:
            logger.warning(
                "chat_with_tools: agent={} ignored invalid hyperparams: {}",
                agent_name, ignored_keys,
            )
        logger.info(
            "chat_with_tools: agent={} merged hyperparams: {}",
            agent_name, list(clean_agent_sampling.keys()),
        )

    tool_names = [d.get("function", {}).get("name", "?") for d in (tool_defs or [])]

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
        reasoning_effort=sampling.get("reasoning_effort") if sampling else None,
        sampling_params=sampling,
        on_tool_start=on_tool_start_override or default_start,
        on_tool_result=on_tool_result_override or default_result,
        on_iteration_usage=_on_iter_usage,
        on_content_delta=on_content_delta,
        on_content_reset=on_content_reset,
        clean_response=clean_response,
        result_max_chars=_direct_result_max,
    )

    content = result.content or ""
    stats = build_stats(result, effective_defs, tool_names, messages_snap, sampling, max_tokens)

    logger.info(
        "chat_with_tools result: agent={} iters={} latency={:.2f}s "
        "tokens={} tools_used={} finish={} content={}",
        agent_name, result.iterations, result.latency,
        result.token_usage, result.tools_used, result.finish_reason, content,
    )

    return content, result.tools_used, stats
