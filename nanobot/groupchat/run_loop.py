"""Main group chat event loop and summary generation.

Contains the core run loop that processes user input and dispatches
to broadcast mode, plus the summary generator.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.groupchat import display as _d
# ── 关键修复：import 移到文件顶部，避免循环内重复 import ──
from nanobot.groupchat.broadcast import broadcast_round


async def generate_summary(engine: Any) -> None:
    """Generate a discussion summary using the first active agent's model."""
    if not engine._history:
        return

    # ── 关键修复：防止 _active_agents 为空时 IndexError ──
    if not engine._active_agents:
        await engine._send("⚠️ 没有活跃 agent，无法生成总结")
        return

    agent_name = engine._active_agents[0]
    model = engine.registry[agent_name]["model"]

    try:
        response = await engine.provider.chat_with_retry(
            messages=[
                {"role": "system", "content": "你是一个讨论总结专家。"},
                {"role": "user", "content": (
                    f"话题：{engine._topic}\n\n"
                    f"群聊记录：\n{engine._format_history()}\n\n"
                    "请输出简洁总结：1)核心观点 2)分歧点 3)初步结论"
                )},
            ],
            model=model,
            max_tokens=2000,
        )
        summary = response.content or "无法生成总结"
        await engine._send(f"📋 讨论总结:\n\n{summary}")
    except Exception as e:
        logger.error("Summary failed: {}", e)
        await engine._send(f"⚠️ 总结生成失败: {e}")


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

            # ── 关键修复：给 broadcast_round 加上全局超时保护 ──
            # 防止某一轮卡死导致整个群聊永久阻塞
            await broadcast_round(
                speak_order,
                engine,
                engine._mailbox,
                global_timeout=600.0,   # 10 分钟（可根据需要调整）
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