"""Direct 1-on-1 chat mode for group chat engine.

Handles single-agent conversations when exactly one agent is active.
Manages session setup, message building, streaming, and response handling.

Supports user interjection: after the agent replies, the loop waits
briefly for new user messages (via engine._direct_chat_queue).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.groupchat.display.streaming import StreamingDisplay
from nanobot.groupchat.orchestra.chat_utils import (
    build_tool_log,
    log_request,
    reasoning_tokens_from_provider_meta,
)

_MAX_CYCLES = 999


async def direct_chat(engine: Any, user_message: str) -> str | None:
    """Send message to single active agent (1-on-1 mode) with interjection."""
    if len(engine._active_agents) != 1:
        return None

    agent_name = engine._active_agents[0]
    agent = engine.registry[agent_name]

    if not engine._session_dir:
        from nanobot.utils.helpers import cn_now as _cn_now_local
        timestamp = _cn_now_local().strftime("%Y%m%d-%H%M%S")
        sessions_dir = Path.home() / ".nanobot" / "collab-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        engine._session_dir = sessions_dir / f"gc-{timestamp}"
        engine._session_dir.mkdir(parents=True, exist_ok=True)
        engine._save_event("session_start", extra={
            "agents": [agent_name],
            "mode": "direct",
            "topic": engine._topic or "",
            "leader": None,
            "models": {agent_name: agent.get("model", "?")},
        })

    messages: list[dict[str, Any]] = engine.prompt_builder.build_agent_prompt(
        agent_name,
        registry=engine.registry,
        active_agents=[agent_name],
        history=engine._history,
        leader=None,
        round_num=0,
    )
    messages.append({"role": "user", "content": user_message})

    cycle = 0
    current_user_msg = user_message
    last_response: str | None = None
    content = ""

    while cycle < _MAX_CYCLES:
        cycle += 1

        _header = f"💬 {agent_name}:\n\n"
        stream = StreamingDisplay(_header, engine._send_and_get_id_fn, engine._edit_fn)
        _delta_cb = stream.on_delta if stream.enabled else None
        _reset_cb = stream.on_reset if stream.enabled else None

        _dc_total_chars = sum(
            len(m.get("content", "")) if isinstance(m.get("content"), str)
            else sum(len(b.get("text", "")) for b in m.get("content", []) if isinstance(b, dict))
            if isinstance(m.get("content"), list) else 0
            for m in messages
        )
        logger.info(
            "direct_chat [{}] cycle {} start: msgs={} total_chars={} user_msg={}",
            agent_name, cycle, len(messages), _dc_total_chars, current_user_msg,
        )

        try:
            content, tools_used, stats = await engine._chat_with_tools(
                messages=messages,
                model=agent["model"],
                agent_name=agent_name,
                is_direct=True,
                on_content_delta=_delta_cb,
                on_content_reset=_reset_cb,
            )
            log_request(engine, agent_name, agent["model"], "direct",
                        reply_len=len(content), msgs=len(messages),
                        tools=tools_used,
                        input_preview=current_user_msg, output=content,
                        **stats)
            logger.info(
                "direct_chat [{}] cycle {} result: content={}",
                agent_name, cycle, content,
            )
            _tool_details = stats.get("tool_calls_detail", [])
            if content or _tool_details:
                engine._add_message("用户", current_user_msg)
                history_content = (content or "") + build_tool_log(_tool_details)
                engine._add_message(agent_name, history_content)
                await engine._maybe_compress_history()
                tok = stats.get("tokens", {})
                total = tok.get("total", 0)
                display_content = content or "[仅调用了工具，无文字回复]"
                if total > 0:
                    p, c = tok.get("prompt", 0), tok.get("completion", 0)
                    cost = stats.get("cost", 0) or 0
                    cache_t = stats.get("cache_tokens", 0) or 0
                    reasoning_t = reasoning_tokens_from_provider_meta(stats.get("provider_meta"))
                    stat_line = _d.format_token_stats(
                        p, c, cost=cost, cache_tokens=cache_t, reasoning_tokens=reasoning_t,
                    )
                    display_content = f"{display_content}\n\n{stat_line}"
                await stream.finalize(display_content, fallback_send=engine._send)
                last_response = content or (
                    "[仅调用了工具，无文字回复]" if _tool_details else ""
                )
            else:
                if stream.msg_id and engine._edit_fn:
                    try:
                        await engine._edit_fn(stream.msg_id, f"⚠️ {agent_name} 返回空回复")
                    except Exception:
                        pass
                else:
                    await engine._send(f"⚠️ {agent_name} 返回空回复 (模型可能暂时异常，请重试)")
                break

        except Exception as e:
            logger.error("Direct chat with {} failed: {}", agent_name, e)
            log_request(engine, agent_name, agent["model"], "direct",
                        msgs=len(messages),
                        error=str(e))
            await engine._send(f"⚠️ {agent_name} 回复失败: {e}")
            break

        try:
            new_msg = await asyncio.wait_for(
                engine._direct_chat_queue.get(), timeout=1.0,
            )
        except asyncio.TimeoutError:
            break

        logger.info("Direct chat: interjection received ({} chars), cycle {}", len(new_msg), cycle)
        await engine._send("── 插话 ──")

        current_user_msg = new_msg

        if content:
            messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": new_msg})

    return last_response