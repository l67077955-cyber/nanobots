"""Agent speaking logic for group chat.

Handles a single agent's turn: prompt building, synthesis context injection,
debug context logging, streaming display, tool calling, error handling,
and message history management.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.streaming import StreamingDisplay
from nanobot.groupchat.utils import build_tool_log, log_request


async def agent_speak(
    engine: Any,
    agent_name: str,
    synthesis_context: str | None = None,
    no_tools: bool = False,
    no_stream: bool = False,
    silent: bool = False,
) -> tuple[str, list[str], dict] | None:
    """Run one agent's turn. Returns (content, tools_used, stats) or None on error.

    Args:
        engine: GroupChatEngine instance.
        agent_name: Name of the speaking agent.
        synthesis_context: Optional research summary injected before the
            agent's own prompt (used for leader synthesis in parallel mode).
        no_tools: If True, disable tool calling (forces pure text response).
        no_stream: If True, disable streaming.
        silent: If True, suppress all internal display.
    """
    if agent_name not in engine.registry:
        return None
    model = engine.registry[agent_name]["model"]
    messages = engine._build_agent_prompt(agent_name)

    # Inject synthesis context for leader (before the final nudge)
    if synthesis_context:
        insert_pos = max(len(messages) - 1, 0)
        messages.insert(insert_pos, {
            "role": "system",
            "content": synthesis_context,
        })

    # ── Context size breakdown (only when /debug enabled) ──
    if engine._debug_context:
        total_chars = 0
        parts: list[str] = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "?")
            name = msg.get("name", "")
            content = msg.get("content", "")
            c_len = len(content) if isinstance(content, str) else sum(
                len(b.get("text", "")) for b in content if isinstance(b, dict)
            ) if isinstance(content, list) else 0
            total_chars += c_len
            label = (content[:30] if isinstance(content, str) else "").replace("\n", " ")
            tag = f"{name}:" if name else ""
            parts.append(f"  [{i}] {role}{':' if tag else ''}{tag} {c_len:,}字 | {label}…")
        logger.info(
            "Context for {} ({}):\n{}\n  ── TOTAL: {:,} chars, {} messages",
            agent_name, model, "\n".join(parts), total_chars, len(messages),
        )

    # ── Streaming ──
    total = len(engine._active_agents)
    idx = engine._active_agents.index(agent_name) + 1 if agent_name in engine._active_agents else 0
    _header = _d.agent_header(agent_name, leader=engine._leader, idx=idx, total=total)

    stream = StreamingDisplay(
        _header, engine._send_and_get_id_fn, engine._edit_fn,
        tool_in_progress_text=_d.tool_in_progress_msg(_header),
    )

    if no_stream:
        _delta_cb = None
        _reset_cb = None
    else:
        _delta_cb = stream.on_delta if stream.enabled else None
        _reset_cb = stream.on_reset if stream.enabled else None

    try:
        content, tools_used, stats = await engine._chat_with_tools(
            messages=messages,
            model=model,
            agent_name=agent_name,
            max_iterations=1 if no_tools else 5,
            on_content_delta=_delta_cb,
            on_content_reset=_reset_cb,
            force_no_tools=no_tools,
        )
        iters = stats.get("iterations", 1)
        latency = stats.get("latency", 0)
        is_error = stats.get("finish_reason") == "error"

        if is_error:
            err_short = content[:150] if content else "Unknown error"
            logger.error("Agent {} LLM error ({}s): {}", agent_name, latency, err_short)
            err_msg = _d.error_msg(agent_name, err_short, latency)
            if stream.msg_id and engine._edit_fn:
                try:
                    await engine._edit_fn(stream.msg_id, err_msg)
                except Exception:
                    await engine._send(err_msg)
            else:
                await engine._send(err_msg)
            log_request(engine, agent_name, model, "group",
                        msgs=len(messages),
                        error=err_short, status_code=stats.get("status_code"),
                        **{k: v for k, v in stats.items() if k not in ("status_code",)})
            return None

        completion_msg = _d.completion_msg(agent_name, latency, iters, tools_used)

        log_request(engine, agent_name, model, "group",
                    reply_len=len(content), msgs=len(messages),
                    tools=tools_used,
                    input_preview=(engine._history[-2]["content"] if len(engine._history) >= 2 else ""),
                    output=content, **stats)
        logger.info(
            "Agent {} result: content_len={} tools={} stream_msg_id={} iters={} latency={} content={}",
            agent_name, len(content), tools_used, stream.msg_id,
            stats.get("iterations"), stats.get("latency"), content,
        )

        # ── Final display ──
        if content:
            history_content = content + build_tool_log(stats.get("tool_calls_detail", []))
            engine._add_message(agent_name, history_content)
            # Append token usage to displayed reply
            tok = stats.get("tokens", {})
            total = tok.get("total", 0)
            display_content = content
            if total > 0:
                p, c = tok.get("prompt", 0), tok.get("completion", 0)
                cost = stats.get("cost", 0) or 0
                cache_t = stats.get("cache_tokens", 0) or 0
                cost_str = f" ${cost:.4f}" if cost else ""
                cache_str = f" 🔵{cache_t}" if cache_t else ""
                display_content = f"{content}\n\n`in:{p} out:{c} Σ{total}{cost_str}{cache_str}`"
            if not silent:
                await stream.finalize(display_content, fallback_send=engine._send)
        elif not silent:
            await stream.finalize("", fallback_send=engine._send)
            logger.warning("Agent {} returned empty content", agent_name)

        if completion_msg and not silent:
            await engine._send(completion_msg)
        return (content, tools_used, stats)
    except Exception as e:
        logger.error("Groupchat: {} LLM call failed: {}", agent_name, e)
        log_request(engine, agent_name, model, "group",
                    msgs=len(messages),
                    error=str(e))
        await engine._send(_d.error_msg(agent_name, str(e)))
        return None
