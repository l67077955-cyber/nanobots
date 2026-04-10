"""Direct 1-on-1 chat mode for group chat engine.

Handles single-agent conversations when exactly one agent is active.
Manages session setup, message building, streaming, and response handling.

Supports user interjection: after the agent replies, the loop waits
briefly for new user messages (via engine._direct_chat_queue).  If the
user "interrupts" before the agent finishes or sends a follow-up right
after, the agent immediately continues with the new context — similar
to how broadcast mode's _user_listener works.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.prompt_builder import PromptBuilder
from nanobot.groupchat.streaming import StreamingDisplay
from nanobot.groupchat.utils import build_tool_log, cn_now as _cn_now, log_request


# Maximum follow-up cycles (safety cap to prevent infinite loops)
_MAX_CYCLES = 999  # effectively unlimited


async def direct_chat(engine: Any, user_message: str) -> str | None:
    """Send message to single active agent (1-on-1 mode) with interjection.

    Runs the first reply, then loops waiting for user interjections
    via ``engine._direct_chat_queue``.  Exits when no interjection
    arrives within the timeout window.

    Args:
        engine: GroupChatEngine instance.
        user_message: The user's message text.

    Returns:
        Response text to send (or None if already sent via streaming).
    """
    if len(engine._active_agents) != 1:
        return None

    agent_name = engine._active_agents[0]
    agent = engine.registry[agent_name]

    # Ensure session directory exists
    if not engine._session_dir:
        from nanobot.groupchat.utils import cn_now as _cn_now_local
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

    # Build messages: system(persona) → [memory] → [skills] → [history] → user(new)
    now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
    messages: list[dict[str, str]] = [
        {"role": "system", "content": agent["prompt"] + f"\n\n[Current date and time: {now}]"},
    ]

    # Long-term memory (progressive loading — pointer only, agent reads via read_file)
    try:
        from nanobot.agent.memory import MemoryStore
        store = MemoryStore(engine.workspace)
        if store.read_long_term().strip():
            messages.append({"role": "system", "content": (
                "[Long-term Memory — 长期记忆]\n\n"
                f"你有持久化记忆文件。用 read_file 查看完整内容：\n"
                f"- `{store.memory_file}` — 长期事实记忆 (MEMORY.md)\n"
                f"- `{store.history_file}` — 时间线日志 (HISTORY.md)"
            )})
    except Exception:
        pass  # memory is optional; don't break chat if unavailable

    # Skills (always-on full content + compact listing of others)
    try:
        from nanobot.agent.skills import SkillsLoader
        loader = SkillsLoader(engine.workspace)
        parts: list[str] = []
        always = loader.get_always_skills()
        if always:
            ac = loader.load_skills_for_context(always)
            if ac:
                parts.append(ac)
        summary = loader.build_skills_summary(exclude=set(always) if always else None)
        if summary:
            parts.append("Other skills (read SKILL.md to use):\n" + summary)
        if parts:
            messages.append({"role": "system", "content": "\n\n".join(parts)})
    except Exception:
        pass  # skills are optional

    # Tool instructions (load from .md file → fallback to default)
    tool_hint = PromptBuilder.get_component_template("tool_instructions")
    if tool_hint:
        messages.append({"role": "system", "content": tool_hint})

    # Few-shot examples
    examples = agent.get("examples", "")
    if examples:
        messages.append({"role": "system", "content": f"以下是对话风格示例：\n{examples}"})

    # Chat history
    messages.extend(PromptBuilder.history_to_messages(engine._history, agent_name))

    # Current user message
    messages.append({"role": "user", "content": user_message})

    # Post-history instructions
    instructions = agent.get("instructions", "")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # ── Cycle loop: reply → wait for interjection → reply again ──
    cycle = 0
    current_user_msg = user_message
    last_response: str | None = None

    while cycle < _MAX_CYCLES:
        cycle += 1

        # ── Streaming setup ──
        _header = f"💬 {agent_name}:\n\n"
        stream = StreamingDisplay(_header, engine._send_and_get_id_fn, engine._edit_fn)
        _delta_cb = stream.on_delta if stream.enabled else None
        _reset_cb = stream.on_reset if stream.enabled else None

        # Log full context before LLM call
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
            if content:
                engine._add_message("用户", current_user_msg)
                # Store content with tool call log so model sees its history
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
                    reasoning_t = (stats.get("provider_meta") or {}).get("reasoning_tokens", 0) or 0
                    stat_line = _d.format_token_stats(p, c, cost=cost, cache_tokens=cache_t, reasoning_tokens=reasoning_t)
                    display_content = f"{content}\n\n{stat_line}"
                await stream.finalize(display_content, fallback_send=engine._send)
                last_response = content
            else:
                if stream.msg_id and engine._edit_fn:
                    try:
                        await engine._edit_fn(stream.msg_id, f"⚠️ {agent_name} 返回空回复")
                    except Exception:
                        pass
                else:
                    await engine._send(f"⚠️ {agent_name} 返回空回复 (模型可能暂时异常，请重试)")
                break  # empty reply — stop cycling

        except Exception as e:
            logger.error("Direct chat with {} failed: {}", agent_name, e)
            log_request(engine, agent_name, agent["model"], "direct",
                        msgs=len(messages),
                        error=str(e))
            await engine._send(f"⚠️ {agent_name} 回复失败: {e}")
            break

        # ── Wait for user interjection ──
        # Drain the queue: pick up any messages queued while agent was talking
        try:
            new_msg = await asyncio.wait_for(
                engine._direct_chat_queue.get(), timeout=1.0,
            )
        except asyncio.TimeoutError:
            # No interjection — normal exit
            break

        # Got an interjection! Log and continue the cycle.
        logger.info("Direct chat: interjection received ({} chars), cycle {}", len(new_msg), cycle)
        await engine._send(f"── 插话 ──")

        current_user_msg = new_msg

        # Inject the agent's previous reply and the new user message
        # into the messages list so the agent sees full context
        if content:
            messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": new_msg})

    # Return None — all output already sent via streaming/send
    return None
