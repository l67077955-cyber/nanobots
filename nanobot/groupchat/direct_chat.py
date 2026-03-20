"""Direct 1-on-1 chat mode for group chat engine.

Handles single-agent conversations when exactly one agent is active.
Manages session setup, message building, streaming, and response handling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.groupchat.prompt_builder import PromptBuilder
from nanobot.groupchat.streaming import StreamingDisplay
from nanobot.groupchat.utils import cn_now as _cn_now, log_request


async def direct_chat(engine: Any, user_message: str) -> str | None:
    """Send message to single active agent (1-on-1 mode).

    Uses proper multi-message format with tool calling.

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

    # Build messages: system(persona) → [history] → user(new)
    now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
    messages: list[dict[str, str]] = [
        {"role": "system", "content": agent["prompt"] + f"\n\n[Current date and time: {now}]"},
    ]

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

    # ── Streaming ──
    _header = f"💬 {agent_name}:\n\n"
    stream = StreamingDisplay(_header, engine._send_and_get_id_fn, engine._edit_fn)
    _delta_cb = stream.on_delta if stream.enabled else None
    _reset_cb = stream.on_reset if stream.enabled else None

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
                    input_preview=user_message[:200], output=content[:500],
                    **stats)
        if content:
            engine._add_message("用户", user_message)
            engine._add_message(agent_name, content)
            await stream.finalize(content, fallback_send=engine._send)
            if stream.msg_id:
                return None  # Already sent via streaming
            else:
                return f"{_header}{content}"
        else:
            if stream.msg_id and engine._edit_fn:
                try:
                    await engine._edit_fn(stream.msg_id, f"⚠️ {agent_name} 返回空回复")
                except Exception:
                    pass
                return None
            return f"⚠️ {agent_name} 返回空回复 (模型可能暂时异常，请重试)"
    except Exception as e:
        logger.error("Direct chat with {} failed: {}", agent_name, e)
        log_request(engine, agent_name, agent["model"], "direct",
                    msgs=len(messages),
                    error=str(e))
        return f"⚠️ {agent_name} 回复失败: {e}"
