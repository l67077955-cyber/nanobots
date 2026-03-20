"""Main group chat event loop and summary generation.

Contains the core run loop that processes user input and dispatches
to serial, broadcast, or orchestra modes, plus the summary generator.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.groupchat import display as _d


async def generate_summary(engine: Any) -> None:
    """Generate a discussion summary using the first active agent's model."""
    if not engine._history:
        return
    agent_name = engine._active_agents[0] if engine._active_agents else list(engine.registry.keys())[0]
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

    Dispatches to serial, broadcast, or orchestra mode based on
    engine._mode and engine._leader settings.
    """
    _my_task = asyncio.current_task()
    try:
        await engine._send(
            f"🎭 群聊模式！\n"
            f"👥 成员: {', '.join(engine._active_agents)}\n"
            f"📌 直接发消息，所有 agent 会轮流回复"
        )

        if not any(m["sender"] == "系统" for m in engine._history):
            engine._add_message("系统", f"话题：{engine._topic}")

        rounds = 0
        while engine._running and rounds < engine.config.max_rounds:
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
            current_agents = list(engine._active_agents)
            if engine._leader and engine._leader in current_agents:
                others = [a for a in current_agents if a != engine._leader]
                speak_order = others + [engine._leader]
            else:
                speak_order = current_agents

            # Dispatch to appropriate mode
            if engine._leader and engine._leader in current_agents and len(others) > 0:
                await engine._orchestra_round(speak_order)
            elif engine._mode == "broadcast" and not engine._leader:
                from nanobot.groupchat.broadcast import broadcast_round
                await broadcast_round(speak_order, engine, engine._mailbox)
            else:
                # Serial mode
                for si, name in enumerate(speak_order):
                    if not engine._running or name not in engine._active_agents:
                        break
                    model_short = engine.registry.get(name, {}).get("model", "?").split("/")[-1]
                    await engine._send(_d.thinking_msg(name, model_short, leader=engine._leader, idx=si+1, total=len(speak_order)))
                    await asyncio.sleep(engine.config.auto_reply_delay)
                    await engine._agent_speak(name)

            # Signal round complete
            if engine._on_round_done:
                try:
                    await engine._on_round_done()
                except Exception:
                    pass

        if engine._running:
            await engine._send("🔚 群聊结束！正在生成总结...")
            await generate_summary(engine)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Group chat loop error: {}", e)
        await engine._send(f"❌ 群聊异常: {e}")
    finally:
        if engine._task is _my_task:
            engine._running = False
        logger.info("Group chat loop ended")
