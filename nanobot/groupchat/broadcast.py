"""Broadcast execution mode for group chat.

Runs all agents concurrently with out-of-order display.
Agents can communicate with each other via chatroom_send/wait tools.
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.mailbox import MailboxHub


# Type alias for the engine to avoid circular import
# The actual GroupChatEngine is passed at runtime
_Engine = Any


async def broadcast_round(
    agents: list[str],
    engine: _Engine,
    mailbox: MailboxHub,
    global_timeout: float = 200.0,
) -> list[tuple[str, str | None]]:
    """Run all agents concurrently with out-of-order completion display.

    Each agent:
    1. Gets its own asyncio.Task
    2. Can use chatroom_send/wait to talk to other agents
    3. Results display as each agent finishes (first-done-first-shown)

    Args:
        agents: List of agent names to run.
        engine: The GroupChatEngine instance.
        mailbox: Shared MailboxHub for inter-agent communication.
        global_timeout: Hard limit for the entire round (seconds).

    Returns:
        List of (agent_name, content) tuples in completion order.
    """
    if not agents:
        return []

    # Prepare mailboxes
    for name in agents:
        mailbox.create(name)
    mailbox.start_round()

    total = len(agents)

    # Announce broadcast start
    await engine._send(
        f"📡 广播模式 — {total} 个 Agent 同时启动\n"
        f"👥 {', '.join(agents)}\n"
        f"⏱ 全局超时: {int(global_timeout)}s"
    )

    # ── Build per-agent tool registries with chatroom tools ──
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.groupchat.chatroom_tools import ChatroomSendTool, WaitTool

    agent_tool_registries: dict[str, ToolRegistry] = {}
    for name in agents:
        # Clone the engine's group tool registry and add chatroom tools
        registry = ToolRegistry()
        # Copy existing tools from default registry
        for tool_name in engine.tools.tool_names:
            tool = engine.tools.get(tool_name)
            if tool:
                registry.register(tool)
        # Add chatroom tools (per-agent instances)
        send_tool = ChatroomSendTool(mailbox=mailbox, agent_name=name)
        wait_tool = WaitTool(mailbox=mailbox, agent_name=name)
        registry.register(send_tool)
        registry.register(wait_tool)
        agent_tool_registries[name] = registry

    # ── Run each agent as a concurrent task ──

    async def _run_one(
        name: str,
        agent_idx: int,
    ) -> tuple[str, str | None, list[str], dict]:
        """Run a single agent with streaming display."""
        if name not in engine.registry:
            return (name, None, [], {})

        agent_cfg = engine.registry[name]
        model = agent_cfg["model"]
        model_short = model.split("/")[-1]
        messages = engine._build_agent_prompt(name)

        # ── Streaming state ──
        _stream_msg_id: int | None = None
        _stream_buffer: list[str] = []
        _tool_lines: list[str] = []
        _last_edit: float = 0.0
        _EDIT_INTERVAL = 0.8

        badge = f" [{agent_idx + 1}/{total}]"
        _header = f"📡 {name}{badge}:\n\n"

        # Send initial "thinking" message
        if engine._send_and_get_id_fn:
            _stream_msg_id = await engine._send_and_get_id_fn(
                f"⏳ {name} 思考中... ({model_short})"
            )

        # ── Streaming callbacks ──

        async def _on_delta(delta: str) -> None:
            nonlocal _last_edit
            _stream_buffer.append(delta)
            now = _time.time()
            if _stream_msg_id and engine._edit_fn and (now - _last_edit) >= _EDIT_INTERVAL:
                activity = "\n".join(_tool_lines) + "\n\n" if _tool_lines else ""
                text = f"{_header}{activity}" + "".join(_stream_buffer) + " ▍"
                try:
                    await engine._edit_fn(_stream_msg_id, text[:4096])
                except Exception:
                    pass
                _last_edit = now

        async def _on_reset() -> None:
            _stream_buffer.clear()

        _TOOL_ICONS = {
            "web_search": "🔍", "web_fetch": "🌐", "exec": "⚡",
            "read_file": "📖", "write_file": "✏️", "edit_file": "✏️",
            "list_dir": "📁", "chatroom_send": "💬", "wait": "⏳",
        }

        async def _on_tool_start(tool_name: str, args: dict) -> None:
            if not isinstance(args, dict):
                args = {}
            icon = _TOOL_ICONS.get(tool_name, "🔧")
            if tool_name == "chatroom_send":
                to = args.get("to", "?")
                msg_preview = (args.get("message", "") or "")[:40]
                _tool_lines.append(f"{icon} → {to}: {msg_preview}")
            elif tool_name == "wait":
                from_who = args.get("from_agent", "")
                t = args.get("timeout", 30)
                _tool_lines.append(f"{icon} 等待{'来自 ' + from_who if from_who else '消息'} ({t}s)")
            elif tool_name == "web_search":
                query = args.get("query", "")
                _tool_lines.append(f"{icon} 搜索: {query}")
            elif tool_name == "web_fetch":
                url = (args.get("url", "") or "")[:50]
                _tool_lines.append(f"{icon} 浏览: {url}")
            else:
                short = ""
                if args:
                    first = list(args.values())[0]
                    if isinstance(first, str):
                        short = first[:40]
                _tool_lines.append(f"{icon} {tool_name}" + (f" {short}" if short else ""))

            # Update consolidated message
            if _stream_msg_id and engine._edit_fn:
                text = f"{_header}" + "\n".join(_tool_lines)
                try:
                    await engine._edit_fn(_stream_msg_id, text[:4096])
                except Exception:
                    pass

        async def _on_tool_result(tool_name: str, tool_call_id: str, result: str) -> None:
            if not result or not _tool_lines:
                return
            rlen = len(result)
            preview = result.strip().replace("\n", " ")[:60]
            _tool_lines[-1] += f"\n  ↳ {preview}{'…' if rlen > 60 else ''}"
            if _stream_msg_id and engine._edit_fn:
                text = f"{_header}" + "\n".join(_tool_lines)
                try:
                    await engine._edit_fn(_stream_msg_id, text[:4096])
                except Exception:
                    pass

        # ── Determine tool definitions ──
        reg = agent_tool_registries[name]
        tool_defs = engine._get_agent_tools(agent_cfg, reg)
        # Always include chatroom tools for broadcast mode
        chatroom_defs = [
            t.to_schema() for t in [reg.get("chatroom_send"), reg.get("wait")]
            if t is not None
        ]
        if tool_defs:
            # Merge chatroom tools if not already present
            existing_names = {d["function"]["name"] for d in tool_defs}
            for cd in chatroom_defs:
                if cd["function"]["name"] not in existing_names:
                    tool_defs.append(cd)
        else:
            tool_defs = chatroom_defs

        _delta_cb = _on_delta if (engine._edit_fn and engine._send_and_get_id_fn) else None
        _reset_cb = _on_reset if _delta_cb else None

        # ── Run the tool loop ──
        from nanobot.agent.tool_loop import tool_loop

        try:
            result = await tool_loop(
                provider=engine.provider,
                messages=messages,
                tool_registry=reg,
                model=model,
                max_tokens=engine.config.max_tokens,
                max_iterations=5,
                tool_defs=tool_defs if tool_defs else None,
                metadata={
                    "trace_name": f"broadcast_{name}",
                    "trace_user_id": "groupchat",
                    "tags": [name, "broadcast"],
                    "generation_name": f"{name}_broadcast",
                    "debug_context": engine._debug_context,
                    "log_agent": name,
                    "log_mode": "broadcast",
                },
                on_tool_start=_on_tool_start,
                on_tool_result=_on_tool_result,
                on_content_delta=_delta_cb,
                on_content_reset=_reset_cb,
                clean_response=lambda c: engine._clean_response(c, name),
                result_max_chars=20_000,
            )

            content = result.content or ""
            is_error = result.finish_reason == "error"
            latency = result.latency

            if is_error:
                err_short = content[:150] if content else "Unknown error"
                if _stream_msg_id and engine._edit_fn:
                    try:
                        await engine._edit_fn(
                            _stream_msg_id,
                            f"{_header}⚠️ 失败 ({latency}s): {err_short}"
                        )
                    except Exception:
                        pass
                engine._request_log.append({
                    "agent": name, "model": model,
                    "reply_len": 0, "time": engine._history[-1]["content"][:50] if engine._history else "",
                    "mode": "broadcast", "error": err_short,
                    "iterations": result.iterations, "latency": latency,
                })
                return (name, None, [], {})

            # ── Final display ──
            tools_used = result.tools_used
            activity = "\n".join(_tool_lines) + "\n\n" if _tool_lines else ""

            if content:
                engine._add_message(name, content)
                final = f"{_header}{activity}{content}"
                if _stream_msg_id and engine._edit_fn:
                    try:
                        await engine._edit_fn(_stream_msg_id, final[:4096])
                    except Exception:
                        await engine._send(final[:4096])
                else:
                    await engine._send(final[:4096])
            elif _stream_msg_id and engine._edit_fn:
                try:
                    await engine._edit_fn(
                        _stream_msg_id,
                        f"{_header}{activity}(空回复)" if activity else f"{_header}(空回复)",
                    )
                except Exception:
                    pass

            # Completion notification
            if tools_used:
                await engine._send(
                    f"✅ {name} 完成 ({latency}s, {result.iterations}次迭代, "
                    f"工具: {', '.join(tools_used)})"
                )
            elif latency > 0:
                await engine._send(f"✅ {name} 完成 ({latency}s)")

            engine._request_log.append({
                "agent": name, "model": model,
                "reply_len": len(content), "time": _time.strftime("%H:%M:%S"),
                "mode": "broadcast", "tools": tools_used,
                "iterations": result.iterations, "latency": latency,
            })
            return (name, content, tools_used, {})

        except Exception as e:
            logger.error("Broadcast: {} failed: {}", name, e)
            if _stream_msg_id and engine._edit_fn:
                try:
                    await engine._edit_fn(_stream_msg_id, f"{_header}⚠️ 失败: {e}")
                except Exception:
                    pass
            engine._request_log.append({
                "agent": name, "model": model,
                "reply_len": 0, "time": _time.strftime("%H:%M:%S"),
                "mode": "broadcast", "error": str(e),
            })
            return (name, None, [], {})

    # ── Launch all agents concurrently ──
    tasks = {
        asyncio.create_task(_run_one(name, idx)): name
        for idx, name in enumerate(agents)
    }

    results: list[tuple[str, str | None]] = []
    completed = 0

    try:
        for coro in asyncio.as_completed(tasks.keys(), timeout=global_timeout):
            try:
                name, content, *_ = await coro
                completed += 1
                results.append((name, content))
                logger.info(
                    "Broadcast: {}/{} done — {} ({})",
                    completed, total, name,
                    f"{len(content)} chars" if content else "empty",
                )
            except Exception as e:
                completed += 1
                logger.error("Broadcast: agent task error: {}", e)
    except asyncio.TimeoutError:
        # Cancel remaining tasks
        for task, name in tasks.items():
            if not task.done():
                task.cancel()
                logger.warning("Broadcast: {} cancelled (global timeout)", name)
                await engine._send(f"⏰ {name} 超时取消")

    # ── Round summary ──
    comm_count = len(mailbox.history)
    await engine._send(
        f"📡 广播轮次完成: {completed}/{total} 个 Agent 回复"
        + (f", {comm_count} 条 Agent 间通信" if comm_count > 0 else "")
    )

    # Clean up
    mailbox.clear()

    return results
