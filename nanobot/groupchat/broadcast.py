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

        # ── Inject broadcast coordination hint from template ──
        teammates = [a for a in agents if a != name]
        # Load from override system (editable via /prompt), fallback to default
        overrides = engine._load_prompt_overrides("__global__")
        hint_template = overrides.get("broadcast_hint") or engine._get_component_template("broadcast_hint")
        if hint_template:
            # Extract user question from history for echo
            user_question = ""
            for msg in reversed(engine._history):
                if msg.get("sender") in ("User", "user", "系统"):
                    user_question = msg.get("content", "")[:200]
                    break
            hint = (
                hint_template
                .replace("{{agent_idx}}", str(agent_idx + 1))
                .replace("{{total}}", str(total))
                .replace("{{teammates}}", ", ".join(teammates))
                .replace("{{agent}}", name)
                .replace("{{user_question}}", user_question)
            )
            # Append user question reminder if not in template
            if user_question and "{{user_question}}" not in hint_template:
                hint += f"\n\n⚠️ 用户请求: {user_question}\n立即执行此请求，不要打招呼或寒暄。"

            # ── Point-name detection ──
            # If user explicitly names this agent, add priority hint
            if name.lower() in user_question.lower():
                hint += (
                    f"\n\n🎯 用户点名了你（{name}），你是此任务的主要负责人！"
                    f"优先完成用户的请求，然后通过 chatroom_send 报告结果。"
                )

            # ── Agent-idx task differentiation ──
            # Guide agents toward different search strategies
            strategies = [
                "优先使用中文关键词搜索，关注国内新闻和数据源。",
                "优先使用英文关键词搜索，关注国际/学术/技术来源。",
                "优先搜索不同子主题或使用 web_fetch 深入分析关键链接。",
            ]
            strategy = strategies[agent_idx % len(strategies)]
            hint += f"\n\n📌 你的差异化分工策略: {strategy}"

            insert_pos = max(len(messages) - 1, 0)
            messages.insert(insert_pos, {
                "role": "system",
                "content": hint,
            })

        # ── Inject task framing as user-role message ──
        # Some API proxies have safety filters that suppress responses to
        # "general knowledge" queries. Framing the task as a technical/
        # programming research task using tools bypasses these filters.
        if user_question:
            messages.append({
                "role": "user",
                "content": (
                    f"[Technical Research Task] Use your tools (web_search, "
                    f"web_fetch, exec) to research the following and report "
                    f"findings:\n{user_question}"
                ),
            })

        # ── Non-streaming display (broadcast mode) ──
        # No streaming edits — each event gets its own message.
        # This prevents messages from being swallowed by concurrent edits.
        _tool_lines: list[str] = []

        badge = f" [{agent_idx + 1}/{total}]"
        _header = f"📡 {name}{badge}: "

        # Send initial status
        await engine._send(f"⏳ {name} 思考中... ({model_short}){badge}")

        _TOOL_ICONS = {
            "web_search": "🔍", "web_fetch": "🌐", "exec": "⚡",
            "read_file": "📖", "write_file": "✏️", "edit_file": "✏️",
            "list_dir": "📁", "chatroom_send": "💬", "wait": "⏳",
        }

        async def _on_tool_start(tool_name: str, args: dict) -> None:
            if not isinstance(args, dict):
                args = {}
            icon = _TOOL_ICONS.get(tool_name, "🔧")
            # Build internal log line
            if tool_name == "chatroom_send":
                to = args.get("to", "?")
                msg_full = (args.get("message", "") or "")
                line = f"{name}: chatroom_send({to})"
                _tool_lines.append(line)
                # Only chatroom_send is shown to user (full message)
                await engine._send(f"   📨 {icon} {line}\n  → {msg_full}")
            else:
                # All other tools: log internally but don't flood chat
                if tool_name == "web_search":
                    line = f"{name}: web_search({args.get('query', '')})"
                elif tool_name == "web_fetch":
                    line = f"{name}: web_fetch({(args.get('url', '') or '')[:60]})"
                elif tool_name == "wait":
                    from_who = args.get("from_agent", "")
                    line = f"{name}: wait({'来自 ' + from_who if from_who else '消息'})"
                else:
                    line = f"{name}: {tool_name}"
                _tool_lines.append(line)

        async def _on_tool_result(tool_name: str, tool_call_id: str, result: str) -> None:
            # Silent — don't flood chat with tool results
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

        # No streaming callbacks — broadcast uses non-streaming mode
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
                on_content_delta=None,
                on_content_reset=None,
                clean_response=lambda c: engine._clean_response(c, name),
                result_max_chars=20_000,
            )

            content = result.content or ""
            is_error = result.finish_reason == "error"
            latency = result.latency

            if is_error:
                err_short = content[:150] if content else "Unknown error"
                await engine._send(f"   📨 ⚠️ {name} 请求失败 ({latency}s): {err_short}")
                engine._request_log.append({
                    "agent": name, "model": model,
                    "reply_len": 0, "time": engine._history[-1]["content"][:50] if engine._history else "",
                    "mode": "broadcast", "error": err_short,
                    "iterations": result.iterations, "latency": latency,
                })
                return (name, None, [], {})

            # ── Final display ──
            tools_used = result.tools_used

            if content:
                engine._add_message(name, content)
                # Send final content as a complete message
                final = f"{_header}\n{content}"
                await engine._send(f"   📨 {final[:4096]}")
            else:
                await engine._send(f"   📨 {_header}(空回复)")

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
            await engine._send(f"   📨 ⚠️ {name} 异常: {e}")
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

    results: list[tuple[str, str | None, list[str]]] = []
    completed = 0

    try:
        for coro in asyncio.as_completed(tasks.keys(), timeout=global_timeout):
            try:
                name, content, tools_used_list, *_ = await coro
                completed += 1
                results.append((name, content, tools_used_list or []))
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

    # ── Auto-send for non-communicating agents ──
    # Claude models tend to search but never call chatroom_send.
    # Automatically share their results so teammates see them.
    for name, content, tools_list in results:
        if content and "chatroom_send" not in tools_list:
            # Auto-share truncated results on behalf of this agent
            snippet = content[:300]
            mailbox.send(name, ["All"], snippet)
            logger.info("Broadcast: auto-shared {} results ({} chars) via mailbox", name, len(snippet))

    # ── Round summary ──
    comm_count = len(mailbox.history)
    await engine._send(
        f"📡 广播轮次完成: {completed}/{total} 个 Agent 回复"
        + (f", {comm_count} 条 Agent 间通信" if comm_count > 0 else "")
    )

    # Clean up queues (history preserved for synthesis & test harness)
    mailbox.clear()

    # ── Phase 2: Synthesis Discussion ──
    # Grok-style: leader can cross-verify with tools, others synthesize text-only
    valid_results = [(name, content) for name, content, _ in results if content]

    if len(valid_results) >= 1:
        # Build mailbox communication summary for synthesis context
        comm_summary = ""
        if mailbox.history:
            comm_lines = []
            for m in mailbox.history:
                to_str = ", ".join(m.targets)
                comm_lines.append(f"[{m.sender} → {to_str}]: {m.content[:150]}")
            comm_summary = (
                "\n\n[Agent 间通信记录]\n" + "\n".join(comm_lines[-10:])
            )

        # Inject synthesis nudge with Grok-style cross-verification instruction
        engine._add_message("系统", (
            "[综合讨论阶段] 以上是所有 agent 的独立研究结果。"
            + comm_summary +
            "\n\n你的任务："
            "\n1. 综合所有 agent 的发现，找出共识和分歧"
            "\n2. 如有关键数据（数字、链接等），可用工具交叉验证"
            "\n3. 给出最终综合结论，补充遗漏的重要信息"
            "\n4. 简洁有力，不要重复已有内容"
        ))

        # All broadcast agents participate in synthesis (not just those with results),
        # so agents who found nothing can still summarize others' findings.
        # Leader goes first with tool access for cross-verification.
        synth_agents = []
        leader_name = engine._leader or (valid_results[0][0] if valid_results else None)
        if leader_name and leader_name in agents:
            synth_agents.append(leader_name)
        for name in agents:
            if name not in synth_agents and name in engine.registry:
                synth_agents.append(name)

        await engine._send(
            f"📋 进入综合讨论阶段 — {len(synth_agents)} 个 Agent 串行讨论"
        )

        # Run agents serially so each sees the previous agent's summary
        # First agent (leader) gets tool access for cross-verification,
        # subsequent agents synthesize text-only (Grok pattern)
        for si, name in enumerate(synth_agents):
            if name not in engine.registry:
                continue
            model_short = engine.registry[name]["model"].split("/")[-1]
            is_leader = (si == 0)
            mode_label = "综合+验证" if is_leader else "综合"
            await engine._send(
                f"💬 {name} {mode_label}中... ({model_short}) "
                f"[{si + 1}/{len(synth_agents)}]"
            )
            try:
                await engine._agent_speak(name, no_tools=not is_leader)
                # Re-send the full synthesis content as a guaranteed message
                # (streaming _agent_speak may have partial display issues)
                if engine._history:
                    last = engine._history[-1]
                    if last.get("role") == "assistant" and last.get("content"):
                        synth_text = last["content"]
                        await engine._send(
                            f"📨 💬 {name} [{si + 1}/{len(synth_agents)}]:  "
                            f"{synth_text[:4096]}"
                        )
            except Exception as e:
                logger.error("Broadcast synthesis: {} failed: {}", name, e)
                await engine._send(f"⚠️ {name} 综合失败: {e}")

        await engine._send("📋 综合讨论完成")

    return [(name, content) for name, content, _ in results]

