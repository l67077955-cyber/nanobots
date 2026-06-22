"""Main group chat event loop — dispatches to broadcast mode.

Leader's final text reply (before end_discussion) serves as the
synthesis; no separate summary generation stage exists.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.groupchat.orchestra.broadcast import broadcast_round


async def generate_summary(engine: Any) -> None:
    """Generate and send an AI summary of the entire discussion.

    Called when user sends ``__SUMMARY__`` or when Leader triggers
    ``end_discussion``.  Uses the same provider + model as history
    compression (``tool_results.summarize_model``).
    """
    from nanobot.groupchat.history.history_settings import (
        summarize_model as _get_summarize_model,
        history_summarize_enabled,
    )

    if not engine._history or not history_summarize_enabled():
        return

    messages = list(engine._history)
    if not messages:
        return

    # Format conversation for summarisation
    lines = []
    for m in messages:
        sender = m.get("sender", "?")
        content = m.get("content", "")
        if len(content) > 600:
            content = content[:600] + "…"
        lines.append(f"[{sender}] {content}")

    input_text = "\n".join(lines)
    if len(input_text) > 15000:
        input_text = input_text[-15000:]

    provider = engine._history._provider if engine._history else None
    if provider is None:
        await engine._send(
            f"📋 讨论总结\n"
            f"共 {len(messages)} 条消息\n"
            f"参与: {', '.join(engine._active_agents)}"
        )
        return

    model = _get_summarize_model()
    prompt = (
        f"以下是群聊的完整讨论记录（共 {len(messages)} 条）。\n"
        "请用简洁的中文总结核心内容、关键决策和结论。\n"
        "保留重要的数值、文件路径和具体结论。\n"
        "控制在 400 字以内。\n\n"
        f"{input_text}"
    )
    try:
        response = await provider.chat_with_retry(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=600,
        )
        summary = (response.content or "").strip()
        if summary:
            await engine._send(f"📋 讨论总结\n\n{summary}")
        else:
            await engine._send(f"📋 讨论总结\n共 {len(messages)} 条消息")
    except Exception as e:
        logger.error("generate_summary failed: {}", e)
        await engine._send(f"📋 讨论总结\n共 {len(messages)} 条消息（AI 总结生成失败）")


async def run_loop(engine: Any) -> None:
    """Main group chat loop — runs while 2+ agents are active.

    Dispatches to broadcast mode.
    """
    _my_task = asyncio.current_task()
    try:
        n = len(engine._active_agents)
        # Only announce when there's no pending message (fresh start, not loop restart)
        if engine._input_queue.empty():
            if n >= 2:
                await engine._send(
                    f"🎭 群聊模式！\n"
                    f"👥 成员: {', '.join(engine._active_agents)}\n"
                    f"📌 直接发消息，所有 agent 会轮流回复"
                )
            else:
                await engine._send(
                    f"💬 对话模式\n"
                    f"👤 {engine._active_agents[0]}"
                )

        if not any(m["sender"] == "系统" for m in engine._history):
            engine._add_message("系统", f"话题：{engine._topic}")

        rounds = 0
        while engine._running:
            rounds += 1

            # Wait for user input (block until user sends something)
            user_input = None
            while engine._running:
                try:
                    user_input = await asyncio.wait_for(engine._input_queue.get(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    continue

            if not engine._running or not user_input:
                break

            if user_input == "__SUMMARY__":
                await generate_summary(engine)
                continue

            # Record user message (no echo)
            engine._add_message("用户", user_input)
            engine._round = rounds

            # Determine speaking order
            speak_order = list(engine._active_agents)

            from nanobot.groupchat.orchestra.broadcast_orchestrator import load_groupchat_settings
            _gc = load_groupchat_settings()
            _global_timeout = float(_gc.get("global_timeout", 600))

            await broadcast_round(
                speak_order,
                engine,
                engine._mailbox,
                global_timeout=_global_timeout,
            )

            # Compress history if approaching the message limit
            await engine._maybe_compress_history()

            # Signal round complete
            if engine._on_round_done:
                try:
                    await engine._on_round_done()
                except Exception:
                    pass

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Group chat loop error: {}", e)
        await engine._send(f"❌ 群聊异常: {e}")
    finally:
        if engine._task is _my_task:
            engine._running = False
        # Ensure typing indicator is cleaned up even on cancellation
        if engine._on_round_done:
            try:
                await engine._on_round_done()
            except Exception:
                pass
        logger.info("Group chat loop ended")