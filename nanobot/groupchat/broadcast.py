"""Broadcast execution mode for group chat.

Runs all agents concurrently with out-of-order display.
Agents can communicate with each other via chatroom_send/wait tools.
"""

from __future__ import annotations

import asyncio
import copy
import json as _json
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool
from nanobot.groupchat.utils import build_tool_log, log_request


@runtime_checkable
class BroadcastContext(Protocol):
    """Protocol documenting what broadcast_round needs from the engine.

    Replaces the opaque ``Any`` type, making the implicit dependency explicit.
    """

    # ── Public attributes ──
    registry: dict[str, dict[str, Any]]
    tools: Any  # ToolRegistry
    provider: Any  # LLMProvider
    config: Any  # GroupChatConfig

    # ── Private but accessed by broadcast ──
    _round: int
    _leader: str | None
    _debug_context: bool
    _history: list[dict[str, str]]
    _request_log: list[dict[str, Any]]
    _session_dir: Any

    # ── Methods ──
    def _send(self, text: str) -> Awaitable[None]: ...
    def _save_event(self, event_type: str, *, agent: str = "", content: str = "", extra: dict | None = None) -> None: ...
    def _add_message(self, sender: str, content: str) -> None: ...
    def _save_round_summary(self, round_num: int, agents_responded: int, comm_count: int = 0, duration: float = 0.0) -> None: ...
    def _clean_response(self, content: str, agent_name: str) -> str: ...
    def _build_agent_prompt(self, agent_name: str) -> list[dict[str, Any]]: ...
    def _get_agent_tools(self, agent_cfg: dict, registry: Any) -> list: ...
    def _agent_speak(self, agent_name: str, no_tools: bool = False, no_stream: bool = False, silent: bool = False) -> Awaitable: ...

    @property
    def prompt_builder(self) -> Any: ...


async def broadcast_round(
    agents: list[str],
    engine: BroadcastContext,
    mailbox: MailboxHub,
    global_timeout: float = 3600.0,
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

    import time as _time
    _round_t0 = _time.time()

    # ── Detect leader ──
    leader_name = engine._leader if hasattr(engine, '_leader') else None
    if leader_name and leader_name not in agents:
        leader_name = None

    # ── Session-scoped settings snapshot (restore after round) ──
    _original_settings: dict[str, dict] = {}
    if leader_name:
        for name in agents:
            cfg = engine.registry.get(name, {})
            _original_settings[name] = {
                "tools": copy.deepcopy(cfg.get("tools", {})),
            }

    # All agents participate — leader included as active agent
    exec_agents = list(agents)
    non_leader_agents = [a for a in agents if a != leader_name] if leader_name else list(agents)
    total = len(exec_agents)

    # Announce broadcast start
    engine._save_event("round_start", extra={
        "round": engine._round + 1,
        "agents": list(agents),
        "mode": "broadcast",
        "leader": leader_name,
    })
    await engine._send(_d.broadcast_start_msg(list(agents), int(global_timeout), leader=leader_name))

    # ── Load groupchat settings ──
    _gc_settings_path = Path.home() / ".nanobot" / "groupchat_settings.json"
    _gc_defaults = {"search_initial": 2, "search_earn_interval": 4, "allocate_timeout": 15, "call_timeout": 90}
    gc_settings = dict(_gc_defaults)
    if _gc_settings_path.exists():
        try:
            gc_settings.update(_json.loads(_gc_settings_path.read_text()))
        except Exception:
            pass

    # ── Extract user question (for hint injection) ──
    user_question = ""
    for msg in reversed(engine._history):
        if msg.get("sender") in ("User", "user", "用户", "系统"):
            user_question = msg.get("content", "")[:300]
            break

    # ═══════════════════════════════════════════════════════════════
    # Agent Execution (broadcast) — leader runs as active agent
    # ═══════════════════════════════════════════════════════════════

    # ── ConversationPool: OS-style resource pool ──
    n = len(exec_agents)
    # Pool capacity: from settings or auto-calculated
    pool_capacity_setting = gc_settings.get("context_pool_capacity", 0)
    pool_capacity = pool_capacity_setting if pool_capacity_setting > 0 else max(n * (n - 1), 2)
    pool = ConversationPool(capacity=pool_capacity, agents=list(exec_agents))
    pool.ALLOCATE_TIMEOUT = float(gc_settings["allocate_timeout"])
    await engine._send(f"── threads {_d.thread_bar(0, pool_capacity)} ──")

    # ── Build per-agent tool registries with chatroom tools ──
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.base import Tool
    from nanobot.groupchat.chatroom_tools import (
        ChatroomSendTool, WaitTool, CachedSearchTool, SearchPool, LeaderGate,
    )

    agent_tool_registries: dict[str, ToolRegistry] = {}

    # ── Shared search cache + pool ──
    _search_cache: dict[str, tuple[str, str]] = {}
    # SearchPool: use context_points_per_agent if set, else search_initial
    points_per_agent = gc_settings.get("context_points_per_agent", 0)
    search_initial = points_per_agent if points_per_agent > 0 else gc_settings["search_initial"]
    search_pool = SearchPool(
        agents=list(exec_agents),
        initial_per_agent=search_initial,
        earn_interval=gc_settings["search_earn_interval"],
    )

    # ── Shared leader gate (enforces 1-msg-then-wait for non-leaders) ──
    leader_gate: LeaderGate | None = None
    if leader_name:
        leader_gate = LeaderGate(leader_name)

    for name in exec_agents:
        # Get per-agent registry (respects workspace_scope), clone and add chatroom tools
        base_reg = engine._get_agent_registry(name)
        registry = ToolRegistry()
        # Copy existing tools, wrapping web_search with cache
        for tool_name in base_reg.tool_names:
            tool = base_reg.get(tool_name)
            if tool:
                if tool_name == "web_search":
                    registry.register(CachedSearchTool(tool, name, _search_cache, search_pool=search_pool))
                elif tool_name not in ("chatroom_send", "wait"):
                    registry.register(tool)
        # Add chatroom tools (per-agent instances with ConversationPool)
        send_tool = ChatroomSendTool(
            mailbox=mailbox, agent_name=name, pool=pool,
            search_pool=search_pool, leader_gate=leader_gate,
        )
        wait_tool = WaitTool(mailbox=mailbox, agent_name=name, pool=pool)
        wait_tool._send_tool = send_tool
        registry.register(send_tool)
        registry.register(wait_tool)
        agent_tool_registries[name] = registry

    # ── Leader-specific tools: manage_agent + end_discussion + transfer_credits + clear_context ──
    leader_end_event = asyncio.Event()
    _leader_agent_tasks: dict = {}  # populated after tasks are created

    # spawn_fn: called by ManageAgentTool.restart to re-create a task
    def _spawn_agent_task(name: str, idx: int) -> asyncio.Task:
        """Re-spawn a single agent task (used by manage_agent restart action)."""
        task = asyncio.create_task(_run_one(name, idx))
        tasks[task] = name
        # Add to all_tasks set so the main wait loop tracks it
        all_tasks.add(task)
        return task

    if leader_name and leader_name in agent_tool_registries:
        from nanobot.groupchat.chatroom_tools import (
            ManageAgentTool, EndDiscussionTool, TransferCreditsTool, ClearContextTool,
        )
        manage_tool = ManageAgentTool(
            exec_agents=non_leader_agents,
            agent_tasks=_leader_agent_tasks,
            engine=engine,
            mailbox=mailbox,
            spawn_fn=_spawn_agent_task,
        )
        end_tool = EndDiscussionTool(end_event=leader_end_event, engine=engine)
        transfer_tool = TransferCreditsTool(search_pool=search_pool, engine=engine)
        clear_ctx_tool = ClearContextTool(
            engine=engine,
            mailbox=mailbox,
            exec_agents=non_leader_agents,
        )
        agent_tool_registries[leader_name].register(manage_tool)
        agent_tool_registries[leader_name].register(end_tool)
        agent_tool_registries[leader_name].register(transfer_tool)
        agent_tool_registries[leader_name].register(clear_ctx_tool)

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
        # In broadcast mode each agent only sees its own prior turns in history.
        # User/system messages are always kept; other agents' verbose outputs
        # are filtered out to reduce noise and context bloat.
        messages = engine._build_agent_prompt(name, relevant_agents=[name])

        is_leader = (name == leader_name)

        # ── Inject broadcast coordination hint from template ──
        teammates = [a for a in agents if a != name]
        # Load from override system (editable via /prompt), fallback to default
        overrides = engine.prompt_builder._load_prompt_overrides("__global__")

        if is_leader:
            # ── Leader prompt: active orchestrator ──
            agent_caps = []
            for a in non_leader_agents:
                a_cfg = engine.registry.get(a, {})
                a_tools = a_cfg.get("tools", {})
                if isinstance(a_tools, dict):
                    on = [k for k, v in a_tools.items() if v]
                elif a_cfg.get("tools_enabled", False) or a_cfg.get("_default"):
                    on = list(engine.TOOL_NAMES)
                else:
                    on = []
                agent_caps.append(f"  {a}: {', '.join(on) if on else '(无工具)'}")

            leader_hint = (
                f"[Leader 模式 — 你是团队指挥官 👑]\n"
                f"你是 {name}，负责分析问题、分配任务、整合结果。\n\n"
                f"用户请求: {user_question}\n\n"
                f"## 团队成员及工具能力\n"
                + "\n".join(agent_caps) + "\n\n"
                f"## 你的专属工具\n"
                f"- chatroom_send(to, message): 给队友发任务/指令\n"
                f"- wait(): 等待队友汇报结果\n"
                f"- manage_agent(action, agent, ...): 管理队友\n"
                f"    • disable: 踢出并取消该 agent 的任务\n"
                f"    • restart: 将已踢出的 agent 拉回并重新启动（最常用）\n"
                f"    • enable: 仅标记为激活（不重启任务）\n"
                f"    • set_tools: 修改 agent 的工具权限（如 {{\"web_search\": true}}）\n"
                f"    • set_status: 向 agent 注入一条状态消息（修改其下次循环的指令）\n"
                f"- clear_context(agent, keep_last, reason): 清理 agent 的上下文历史\n"
                f"    • 从共享历史移除该 agent 的消息，让其重置思路\n"
                f"    • keep_last=N 可保留最近 N 条不删\n"
                f"- end_discussion(reason): 结束讨论，进入最终总结\n"
                f"- transfer_credits(from_agent, to_agent, amount): 划拨搜索额度\n"
                f"- 你也拥有自己的基础工具（web_search 等），可以自己做部分工作\n\n"
                f"## 搜索额度管理\n"
                f"每个 agent 有独立的搜索额度（{search_pool.status()}）。\n"
                f"没有 web_search 的 agent 的额度闲置，你可以用 transfer_credits 把他们的额度\n"
                f"划拨给有搜索能力的 agent（包括你自己）。\n\n"
                f"## 工作流程\n"
                f"1. 分析问题，决定如何分工\n"
                f"2. 用 chatroom_send 给队友分配具体任务（写清楚要做什么）\n"
                f"   ⚠️ 只分配队友有工具能力完成的任务！无 web_search 的队友不要让他搜索\n"
                f"3. 用 wait() 等待队友回复结果\n"
                f"4. 根据结果：追加任务 / 纠正方向 / 自己补充搜索\n"
                f"5. 信息充分后，先完成以下两步，再调用 end_discussion()：\n"
                f"   a. 在最终文字回复中整合所有发现，给出完整答案\n"
                f"   b. 用 write_file/edit_file 将本次对话的问题、结论、重要信息写入 memory/ 文件\n"
                f"6. 完成记忆写入后，调用 end_discussion() 结束任务\n\n"
                f"## 关键规则\n"
                f"- 发现队友空转或无法完成任务时：果断 end_discussion\n"
                f"- 可以一次给多个队友同时发任务（并行工作）\n"
                f"- 你的最终文字回复就是给用户的答案，要完整、结构化\n"
                f"- ⚠️ 如果你打算自己做搜索/验证，必须先完成工具调用，再调用 end_discussion。\n"
                f"  end_discussion 一旦触发无法撤销，之后再说'我来搜索'只是文字，不会执行。\n"
                f"- ⚠️ 原假设被否证时，不要立即结束。应转向：'那么最近的可验证链条是什么？'\n"
                f"  继续搜索直到能给出正面结论（即使度数更高），而不是仅报告'不成立'。\n"
                f"- ⚠️ 禁止在未写记忆的情况下调用 end_discussion。写记忆 → end_discussion 是强制顺序。\n"
            )
            messages.insert(max(len(messages) - 1, 0), {
                "role": "system",
                "content": leader_hint,
            })
        else:
            # ── Non-leader: standard broadcast hint + wait for leader ──
            hint_template = overrides.get("broadcast_hint") or engine.prompt_builder.get_component_template("broadcast_hint")
            if hint_template:
                hint = (
                    hint_template
                    .replace("{{agent_idx}}", str(agent_idx + 1))
                    .replace("{{total}}", str(total))
                    .replace("{{teammates}}", ", ".join(teammates))
                    .replace("{{agent}}", name)
                    .replace("{{user_question}}", user_question)
                )
                messages.insert(max(len(messages) - 1, 0), {
                    "role": "system",
                    "content": hint,
                })

            # If there's a leader, tell non-leader agents to expect instructions
            if leader_name:
                messages.insert(max(len(messages) - 1, 0), {
                    "role": "system",
                    "content": (
                        f"[团队协作模式 — 严格发言规则]\n"
                        f"Leader {leader_name} 会通过 chatroom_send 给你分配任务。\n\n"
                        f"━━ 发言规则（强制执行）━━\n"
                        f"1. 你每次只能发送 **1 条消息**，然后必须 wait() 等待 Leader 发言\n"
                        f"2. Leader 发言后你的配额重置，可以再发 1 条\n"
                        f"3. 违反此规则的消息会被系统拦截\n"
                        f"4. 有问题必须向 Leader 提出并等待回复\n\n"
                        f"正确流程: 做工作 → chatroom_send(结果) → wait() → 收到 Leader 指令 → 继续"
                    ),
                })

        # ── Inject agent permissions context ──
        perm_lines = []
        for a in exec_agents:
            a_cfg = engine.registry.get(a, {})
            a_tools = a_cfg.get("tools", {})
            if isinstance(a_tools, dict):
                on = [k for k, v in a_tools.items() if v]
            elif a_cfg.get("tools_enabled", False) or a_cfg.get("_default"):
                on = list(engine.TOOL_NAMES)
            else:
                on = []
            extra = ""
            if a == name:
                extra = " ← 你"
            elif a == leader_name:
                extra = " 👑Leader"
            perm_lines.append(f"  {a}: {', '.join(on) if on else '(无工具)'}{extra}")
        perm_hint = (
            "[团队工具权限]\n"
            + "\n".join(perm_lines) + "\n\n"
            "注意：没有 web_search/web_fetch 权限时，也禁止用 exec 执行 curl/wget 等网络命令。\n"
            "如需搜索，请通过 chatroom_send 请求有搜索权限的队友帮忙。"
        )
        messages.insert(max(len(messages) - 1, 0), {
            "role": "system",
            "content": perm_hint,
        })

        # ── Non-streaming display (broadcast mode) ──
        # No streaming edits — each event gets its own message.
        # This prevents messages from being swallowed by concurrent edits.
        _tool_lines: list[str] = []
        _pending_searches: list[str] = []  # Buffer for batching search displays

        badge = f" [{agent_idx + 1}/{total}]"
        _header = f"◍ {name}{badge}: "

        # Send initial status
        await engine._send(_d.thinking_msg(name, model_short, leader=leader_name, idx=agent_idx + 1, total=total))


        async def _flush_searches() -> None:
            """Flush buffered search tool lines as one combined message."""
            if _pending_searches:
                combined = "\n".join(_pending_searches)
                await engine._send(combined)
                _pending_searches.clear()

        async def _on_tool_start(tool_name: str, args: dict) -> None:
            if not isinstance(args, dict):
                args = {}
            # Persist tool_call event to session log
            engine._save_event("tool_call", agent=name, extra={
                "tool": tool_name,
                "args": {k: (v if isinstance(v, str) else v) for k, v in args.items()},
            })
            # Full args logging to server log
            import json as _json_log
            logger.info(
                "broadcast [{}] tool_call: {}({})",
                name, tool_name, _json_log.dumps(args, ensure_ascii=False),
            )
            if tool_name == "chatroom_send":
                # Flush any buffered searches before showing chatroom_send
                await _flush_searches()
                to = args.get("to", "?")
                msg_full = (args.get("message", "") or "")
                to_str = ", ".join(to) if isinstance(to, list) else str(to)
                # Calculate cost for display
                if to_str.lower() == "all":
                    cost = len([a for a in agents if a != name])
                else:
                    cost = len(to) if isinstance(to, list) else 1
                line = f"{name}: chatroom_send({to_str}) [cost={cost}]"
                _tool_lines.append(line)
                # Build stats suffix: token + latency
                import time as _t
                elapsed = _t.time() - _cycle_t0
                tok_t = _cycle_usage.get("total_tokens", 0)
                stats_suffix = ""
                if tok_t > 0:
                    p = _cycle_usage["prompt_tokens"]
                    c = _cycle_usage["completion_tokens"]
                    stats_suffix = "\n" + _d.format_token_stats(p, c, elapsed=elapsed)
                await engine._send(_d.chatroom_send_msg(name, to_str, msg_full + stats_suffix, leader=leader_name))
            elif tool_name == "wait":
                await _flush_searches()
                from_who = args.get("from_agent", "")
                line = f"{name}: wait({'来自 ' + from_who if from_who else '消息'})"
                _tool_lines.append(line)
            elif tool_name in ("web_search", "web_fetch"):
                # Buffer search tools — will be flushed together
                line = _d.tool_activity_msg(name, tool_name, args, leader=leader_name)
                _tool_lines.append(line)
                _pending_searches.append(line)
            else:
                # Non-search tool: flush any pending searches first
                await _flush_searches()
                line = _d.tool_activity_msg(name, tool_name, args, leader=leader_name)
                _tool_lines.append(line)
                await engine._send(line)

        async def _on_tool_result(tool_name: str, tool_call_id: str, result: str) -> None:
            # Persist tool_result event to session log
            engine._save_event("tool_result", agent=name, extra={
                "tool": tool_name,
                "result_len": len(result) if result else 0,
                "success": not (result or "").startswith("Error:"),
            })
            # Full result logging to server log
            logger.info(
                "broadcast [{}] tool_result: {} ({}c): {}",
                name, tool_name, len(result) if result else 0, result,
            )
            # Thread visualization: show status after chatroom_send
            if tool_name == "chatroom_send" and result:
                if "BLOCKED:" in result or "threads]" in result:
                    if "BLOCKED:" in result:
                        await engine._send(
                            f"✗ {name} dropped ── "
                            f"{_d.thread_bar(pool.used, pool.capacity)}"
                        )
                    else:
                        await engine._send(
                            f"  {_d.thread_bar(pool.used, pool.capacity)}"
                        )
            # Show wait results
            elif tool_name == "wait" and result and not result.startswith("⏰"):
                await engine._send(_d.chatroom_wait_msg(name, result, leader=leader_name))
            # Buffer search/fetch results — append to pending batch
            elif tool_name in ("web_search", "web_fetch") and result:
                brief = _d.tool_result_brief(name, tool_name, result)
                if tool_name == "web_search" and search_pool:
                    brief += f"  🔍 {search_pool.status()}"
                _pending_searches.append(brief)
            elif tool_name == "exec" and result:
                await _flush_searches()
                brief = _d.tool_result_brief(name, tool_name, result)
                await engine._send(brief)

        # ── Determine tool definitions ──
        reg = agent_tool_registries[name]
        tool_defs = engine._get_agent_tools(agent_cfg, reg)
        # Always include chatroom + broadcast-specific tools
        broadcast_tool_names = ["chatroom_send", "wait"]
        if is_leader:
            broadcast_tool_names.extend(["manage_agent", "end_discussion", "transfer_credits", "clear_context"])
        broadcast_defs = [
            t.to_schema() for t in [
                reg.get(tn) for tn in broadcast_tool_names
            ]
            if t is not None
        ]
        if tool_defs:
            existing_names = {d["function"]["name"] for d in tool_defs}
            for bd in broadcast_defs:
                if bd["function"]["name"] not in existing_names:
                    tool_defs.append(bd)
        else:
            tool_defs = broadcast_defs

        # No streaming callbacks — broadcast uses non-streaming mode
        # ── Run tool-loop + auto-wait cycle ──
        # After tool_loop finishes, agent automatically enters wait().
        # If a teammate message arrives, inject it and re-run tool_loop.
        # Only exits when cancelled (all-agents-wait) or on error.
        from nanobot.agent.tool_loop import tool_loop

        all_tools_used: list[str] = []
        total_iterations = 0
        total_latency = 0.0
        cycle = 0
        # Leader needs more cycles (chatroom_send/wait loops)
        MAX_CYCLES = 6 if is_leader else 4
        agent_max_iters = 12 if is_leader else 8

        try:
            while cycle < MAX_CYCLES:
                # Respect /stop — exit immediately if engine is no longer running
                if not engine._running:
                    logger.info("Broadcast: {} exiting — engine stopped", name)
                    break
                cycle += 1
                import time as _t
                _cycle_t0 = _t.time()
                _cycle_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

                # Reset per-cycle search limit at the start of each tool_loop cycle
                _search_tool = reg.get("web_search")
                if _search_tool and hasattr(_search_tool, "reset_cycle"):
                    _search_tool.reset_cycle()

                async def _on_iter_usage(usage: dict) -> None:
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        _cycle_usage[k] += usage.get(k, 0)

                result = await tool_loop(
                    provider=engine.provider,
                    messages=messages,
                    tool_registry=reg,
                    model=model,
                    max_tokens=engine.config.max_tokens,
                    max_iterations=agent_max_iters,
                    tool_defs=tool_defs if tool_defs else None,
                    reasoning_effort=agent_cfg.get("reasoning_effort") or None,
                    metadata={
                        "trace_name": f"broadcast_{name}_c{cycle}",
                        "trace_user_id": "groupchat",
                        "tags": [name, "broadcast"],
                        "generation_name": f"{name}_broadcast",
                        "debug_context": engine._debug_context,
                        "log_agent": name,
                        "log_mode": "broadcast",
                    },
                    on_tool_start=_on_tool_start,
                    on_tool_result=_on_tool_result,
                    on_iteration_usage=_on_iter_usage,
                    on_content_delta=None,
                    on_content_reset=None,
                    clean_response=lambda c: engine._clean_response(c, name),
                    result_max_chars=20_000,
                    call_timeout=float(gc_settings.get("call_timeout", 90)) or None,
                )

                # Flush any remaining buffered search lines
                await _flush_searches()

                content = result.content or ""
                is_error = result.finish_reason == "error"
                latency = result.latency
                total_latency += latency
                total_iterations += result.iterations
                all_tools_used.extend(result.tools_used or [])

                if is_error:
                    err_short = content[:150] if content else "Unknown error"
                    await engine._send(f"  ✗ {name} failed ({latency:.1f}s): {err_short}")
                    log_request(engine, name, model, "broadcast",
                                error=err_short, iterations=total_iterations,
                                latency=total_latency)
                    
                    # Broadcast the error to other agents to prevent them from waiting forever
                    error_msg = f"⚠️ [System Alert] I encountered a fatal error and my process has crashed. Error details:\n{err_short}"
                    engine._add_message(name, error_msg)
                    mailbox.send(name, ["All"], error_msg)
                    
                    return (name, None, [], {})

                # Record final text in history
                if content:
                    logger.info(
                        "broadcast [{}] cycle {} output ({}c): {}",
                        name, cycle, len(content), content,
                    )
                    history_content = content + build_tool_log(result.tool_calls_detail)
                    engine._add_message(name, history_content)
                    # Track output for search pool credit recovery
                    search_pool.on_output(name)

                # ── Anti-idle guard: force re-entry if agent did nothing ──
                # If this is cycle 1 and the agent produced no content and used
                # no substantive tools (web_search, web_fetch, exec, etc.),
                # the model is being lazy. Inject a forcing prompt and retry.
                _substantive_tools = {"web_search", "web_fetch", "exec", "read_file", "write_file"}
                if cycle == 1 and not content and not (set(result.tools_used or []) & _substantive_tools):
                    logger.warning(
                        "Broadcast: {} idle on cycle 1 (no content, tools={}), forcing retry",
                        name, result.tools_used,
                    )
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[⚠️ 你（{name}）还没有采取任何行动！]\n"
                            "你必须立即使用工具（web_search, web_fetch, exec 等）来回答用户的最新问题。\n"
                            "不要直接从之前的对话中回答 — 用户需要新的搜索结果。\n"
                            "禁止调用 wait() — 先执行工作再交流。"
                        ),
                    })
                    continue  # skip auto-wait, re-enter tool_loop

                # ── Guard: used tools but produced no text ──
                # Agent ran substantive tools but finished without writing any text.
                # Force a summary cycle so the output is not silently swallowed.
                elif not content and (set(result.tools_used or []) & _substantive_tools) and "chatroom_send" not in (result.tools_used or []):
                    logger.warning(
                        "Broadcast: {} used tools on cycle {} but produced no text (tools={}), forcing summary",
                        name, cycle, result.tools_used,
                    )
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[⚠️ 你（{name}）完成了工具调用，但没有输出任何文字！]\n"
                            "请用自然语言总结工具执行结果，写出你的结论，让 Leader 和队友能看到你的输出。\n"
                            "禁止再调用工具，直接输出文字。"
                        ),
                    })
                    continue  # re-enter tool_loop to produce text

                # ── Leader guard: management-only cycle produced no text ──
                # Leader used manage_agent / end_discussion / transfer_credits but no
                # substantive data tool.  The existing guard above won't fire for these
                # tool names, so the leader silently exits without a synthesis message.
                elif is_leader and not content and result.tools_used \
                        and "chatroom_send" not in (result.tools_used or []) \
                        and not (set(result.tools_used or []) & _substantive_tools):
                    logger.warning(
                        "Broadcast: leader {} management-only cycle {} (tools={}), forcing synthesis",
                        name, cycle, result.tools_used,
                    )
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[⚠️ 你（{name}）完成了管理操作，但没有输出任何文字！]\n"
                            "请立即整合所有队友的发现，给出完整、结构化的最终答案。\n"
                            "这是你作为 Leader 的核心职责，禁止再调用工具，直接输出文字。"
                        ),
                    })
                    continue  # re-enter tool_loop to produce synthesis text

                # ── Auto-wait: enter idle state ──
                # Display the agent's final text for this cycle so it's not swallowed.
                if content:
                    snippet = content[:500]
                    # If this agent hasn't used chatroom_send in this cycle, inject it into the mailbox so others can see it.
                    if not result.tools_used or "chatroom_send" not in result.tools_used:
                        mailbox.send(name, ["All"], snippet)

                    # Append token + latency to displayed reply
                    tok = result.token_usage
                    total_tok = tok.get("total", 0)
                    tok_suffix = ""
                    if total_tok > 0:
                        import time as _t
                        elapsed = _t.time() - _cycle_t0
                        cost = result.cost or 0
                        cache_t = result.cache_tokens or 0
                        reasoning_t = sum(
                            (m.get("reasoning_tokens") or 0)
                            for m in (result.provider_meta or [])
                            if isinstance(m, dict)
                        )
                        tok_suffix = "\n" + _d.format_token_stats(
                            tok.get("prompt", 0), tok.get("completion", 0),
                            elapsed=elapsed, cost=cost, cache_tokens=cache_t,
                            reasoning_tokens=reasoning_t,
                        )
                    
                    target_label = "Broadcast" if (not result.tools_used or "chatroom_send" not in result.tools_used) else "Self/Final"
                    if is_leader:
                        target_label = f"进展 [{cycle}]"
                        
                    await engine._send(_d.chatroom_send_msg(name, target_label, content + tok_suffix, max_len=3000, leader=leader_name))
                    logger.info("Broadcast: displayed {} cycle {} output ({} chars)", name, cycle, len(content))

                # If leader called end_discussion this cycle, exit immediately.
                # Continuing into auto-wait would trigger the all-waiting sentinel,
                # which would re-nudge the leader and cause a repeated end_discussion loop.
                if is_leader and "end_discussion" in (result.tools_used or []):
                    logger.info("Broadcast: leader {} called end_discussion, exiting cycle loop", name)
                    break

                # Now wait for teammate messages
                logger.info("Broadcast: {} entering auto-wait (cycle {})", name, cycle)
                # Release unread pool slots before waiting (mirrors WaitTool behavior)
                # Without this, slots consumed by messages sent TO this agent are never
                # freed, causing pool exhaustion and blocking other agents' replies.
                if pool:
                    pool.release_unread(name)
                msg = await mailbox.wait(name, timeout=60)

                if msg is None:
                    # Timeout — no one talking to us, we're done
                    logger.info("Broadcast: {} auto-wait timeout, exiting", name)
                    # Leader fallback: if no text was produced in this cycle, force a
                    # synthesis pass before exiting so the final answer is never silently
                    # swallowed.  Only do this once (cycle < MAX_CYCLES guards the loop).
                    if is_leader and not content and cycle < MAX_CYCLES:
                        logger.warning(
                            "Broadcast: leader {} auto-wait timeout with no text (cycle {}), forcing synthesis",
                            name, cycle,
                        )
                        messages.append({
                            "role": "system",
                            "content": (
                                f"[最终综合] 等待超时，队友已全部完成。\n"
                                f"请立即综合所有发现，给出完整、结构化的最终答案给用户。\n"
                                f"禁止再调用工具，直接输出文字。"
                            ),
                        })
                        continue  # re-enter tool_loop for synthesis
                    break

                # Got a message! Inject it and re-run tool_loop
                # But first check if /stop was issued while we were waiting
                if not engine._running:
                    logger.info("Broadcast: {} exiting after wait — engine stopped", name)
                    break
                logger.info("Broadcast: {} reactivated by {}: {}", name, msg.sender, msg.content[:60])
                await engine._send(_d.chatroom_wait_msg(name, str(msg), leader=leader_name))
                # Inject agent's own previous output so LLM knows what it already said
                if content:
                    messages.append({
                        "role": "assistant",
                        "content": content,
                    })
                # Anti-repeat injection: remind agent not to repeat itself
                messages.append({
                    "role": "system",
                    "content": (
                        f"[提醒] 你（{name}）已经发表过上述观点。"
                        f"针对队友的新消息做出回应或补充新观点，不要重复已说的内容。"
                    ),
                })
                # Then inject the received teammate message
                messages.append({
                    "role": "user",
                    "content": f"[队友消息] {msg}",
                })

            # ── Final completion ──
            comp = _d.completion_msg(name, round(total_latency, 1), total_iterations, all_tools_used, leader=leader_name)
            if comp:
                await engine._send(comp)

            log_request(engine, name, model, "broadcast",
                        reply_len=len(content) if content else 0,
                        tools=all_tools_used, iterations=total_iterations,
                        latency=round(total_latency, 1))
            return (name, content, all_tools_used, {})

        except asyncio.CancelledError:
            # Cancelled by sentinel (all-agents-waiting) — normal exit
            comp = _d.completion_msg(name, round(total_latency, 1), total_iterations, all_tools_used, leader=leader_name)
            if comp:
                await engine._send(comp)
            return (name, content if 'content' in locals() else "", all_tools_used, {})

        except Exception as e:
            logger.error("Broadcast: {} failed: {}", name, e)
            await engine._send(f"  ✗ {name} error: {e}")
            log_request(engine, name, model, "broadcast",
                        error=str(e))
            return (name, None, [], {})
        finally:
            # Defensive: release any remaining pool slots held by this agent
            # (e.g. if cancelled or errored before auto-wait could release them)
            if pool:
                pool.release_unread(name)
            mailbox.mark_agent_done(name)

    # ── Launch all agents (including leader) concurrently ──
    for name in exec_agents:
        mailbox.create(name)
    mailbox.start_round(active_agents=list(exec_agents))

    tasks = {}
    for idx, name in enumerate(exec_agents):
        tasks[asyncio.create_task(_run_one(name, idx))] = name

    # Register tasks on the engine so remove_agent() can cancel them mid-round
    if hasattr(engine, '_broadcast_tasks'):
        engine._broadcast_tasks.clear()
        for task_obj, task_name in tasks.items():
            engine._broadcast_tasks[task_name] = task_obj

    # Populate _leader_agent_tasks so ManageAgentTool can cancel non-leader tasks
    for task_obj, task_name in tasks.items():
        if task_name != leader_name:
            _leader_agent_tasks[task_obj] = task_name

    results: list[tuple[str, str | None, list[str]]] = []
    completed = 0

    try:
        # ── User interjection listener ──
        _user_listener_running = True

        async def _user_listener() -> None:
            while _user_listener_running:
                try:
                    msg = await asyncio.wait_for(engine._input_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg == "__SUMMARY__":
                    continue

                all_agent_names = list(agents)
                await pool.allocate_user(all_agent_names)

                mailbox.create("用户")
                mailbox.send("用户", ["All"], msg)
                engine._add_message("用户", msg)
                await engine._send(
                    f"── User ──\n{msg}\n"
                    f"  {_d.thread_bar(pool.used, pool.capacity)}"
                )
                logger.info("Broadcast: user interjected: {}", msg[:60])

        user_task = asyncio.create_task(_user_listener())

        # ── Mid-round agent join listener ──
        # Drains engine._pending_join_queue so agents added via /add during
        # an active round are spawned immediately rather than waiting for next round.
        _join_listener_running = True

        async def _join_listener() -> None:
            nonlocal total
            while _join_listener_running:
                try:
                    new_name = await asyncio.wait_for(
                        engine._pending_join_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                # Skip if already running (duplicate notification) or engine stopped
                if new_name in {tasks[t] for t in tasks} or not engine._running:
                    continue
                # Build tool registry for the new agent
                base_reg = engine._get_agent_registry(new_name)
                from nanobot.agent.tools.registry import ToolRegistry
                from nanobot.groupchat.chatroom_tools import (
                    ChatroomSendTool, WaitTool, CachedSearchTool,
                )
                new_reg = ToolRegistry()
                for tool_name in base_reg.tool_names:
                    tool = base_reg.get(tool_name)
                    if tool:
                        if tool_name == "web_search":
                            new_reg.register(CachedSearchTool(tool, new_name, _search_cache, search_pool=search_pool))
                        elif tool_name not in ("chatroom_send", "wait"):
                            new_reg.register(tool)
                send_tool = ChatroomSendTool(
                    mailbox=mailbox, agent_name=new_name, pool=pool,
                    search_pool=search_pool, leader_gate=leader_gate,
                )
                wait_tool = WaitTool(mailbox=mailbox, agent_name=new_name, pool=pool)
                wait_tool._send_tool = send_tool
                new_reg.register(send_tool)
                new_reg.register(wait_tool)
                agent_tool_registries[new_name] = new_reg
                # Register with search pool (initialize credits for new agent)
                with search_pool._lock:
                    search_pool._agents.append(new_name)
                    search_pool._credits[new_name] = search_pool._initial
                    search_pool._searches[new_name] = 0
                    search_pool._outputs[new_name] = 0
                # Register with mailbox
                mailbox.create(new_name)
                mailbox._active_agents.add(new_name)
                idx = total
                total += 1
                new_task = asyncio.create_task(_run_one(new_name, idx))
                tasks[new_task] = new_name
                all_tasks.add(new_task)
                engine._broadcast_tasks[new_name] = new_task
                await engine._send(
                    f"✅ {new_name} 加入当前讨论\n"
                    f"👥 当前成员: {', '.join(engine._active_agents)}"
                )
                logger.info("Broadcast: dynamically spawned {} (idx={})", new_name, idx)

        join_task = asyncio.create_task(_join_listener())

        # Watch for all-agents-waiting (natural conversation end)
        async def _watch_all_waiting() -> None:
            while True:
                await mailbox.all_waiting_event.wait()
                # Grace period: wait 5s and re-check to avoid
                # premature termination when agents briefly pass
                # through wait() between processing cycles
                await asyncio.sleep(5)
                if mailbox._waiting >= mailbox._active_agents and len(mailbox._active_agents) > 0:
                    return  # truly all idle
                # Someone woke up — reset and watch again
                mailbox._all_waiting.clear()
                logger.info("Broadcast: idle sentinel reset — agent(s) reactivated")

        # Watch for leader end_discussion signal
        async def _watch_leader_end() -> None:
            await leader_end_event.wait()

        sentinel = asyncio.create_task(_watch_all_waiting())
        leader_end_sentinel = asyncio.create_task(_watch_leader_end())
        all_tasks = set(tasks.keys()) | {sentinel, leader_end_sentinel}

        while not all(t.done() for t in tasks.keys()):
            done_set, _ = await asyncio.wait(
                [t for t in all_tasks if not t.done()],
                timeout=global_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done_set:
                break

            for t in done_set:
                if t is sentinel:
                    # If a leader is still running, inject a synthesis prompt instead of cancelling.
                    # Non-leaders finishing first removes them from _active_agents, which causes the
                    # sentinel to fire while the leader is mid-cycle (waiting between tool loops).
                    # Cancelling the leader here means no synthesis is ever produced.
                    leader_task = next(
                        (task_obj for task_obj, task_name in tasks.items()
                         if task_name == leader_name and not task_obj.done()),
                        None,
                    ) if leader_name else None
                    if leader_task:
                        # Only nudge if leader hasn't already decided to end.
                        # If leader_end_event is already set, the leader called
                        # end_discussion and is finishing up — re-nudging would
                        # cause the leader to call end_discussion repeatedly in a loop.
                        if not leader_end_event.is_set():
                            logger.info("Broadcast: all non-leader agents idle — nudging leader to synthesize")
                            await engine._send("━━ 队友已完成，等待 Leader 汇总 ━━")
                            mailbox.send(
                                "系统", [leader_name],
                                "所有队友已完成工作并退出。请立即整合所有发现，给出完整的最终答案，然后调用 end_discussion 结束任务。",
                            )
                        else:
                            logger.info("Broadcast: sentinel fired but leader already called end_discussion, skipping nudge")
                        # Discard in both cases — one nudge is enough.
                        all_tasks.discard(t)
                    else:
                        logger.info("Broadcast: all agents waiting, ending round")
                        await engine._send("━━ all agents idle — round complete ━━")
                        for task_obj in tasks:
                            if not task_obj.done():
                                task_obj.cancel()
                        break
                elif t is leader_end_sentinel:
                    logger.info("Broadcast: leader ended discussion")
                    await engine._send("━━ Leader 结束讨论 — entering synthesis ━━")
                    for task_obj, task_name in tasks.items():
                        if not task_obj.done() and task_name != leader_name:
                            task_obj.cancel()
                    # Don't break — let the while loop continue waiting for leader to finish
                elif t in tasks:
                    try:
                        name, content, tools_used_list, *_ = t.result()
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
                        await engine._send(f"\u2717 Agent error: {e}")
            else:
                continue
            break

        # Cancel sentinels if still running
        if not sentinel.done():
            sentinel.cancel()
        if not leader_end_sentinel.done():
            leader_end_sentinel.cancel()

        # Stop user listener and join listener
        _user_listener_running = False
        if not user_task.done():
            user_task.cancel()
        _join_listener_running = False
        if not join_task.done():
            join_task.cancel()

        # Cancel any remaining agent tasks
        for task_obj in tasks:
            if not task_obj.done():
                name = tasks[task_obj]
                task_obj.cancel()
                logger.warning("Broadcast: {} cancelled", name)

        # _run_one catches CancelledError and returns normally, so cancelled tasks
        # still have results. Collect them before computing the round summary.
        pending_cleanup = [t for t in tasks if not t.done()]
        if pending_cleanup:
            done_late, _ = await asyncio.wait(pending_cleanup, timeout=15)
            for t in done_late:
                if t in tasks:
                    try:
                        n, c, tools_l, *_ = t.result()
                        if c:
                            completed += 1
                            results.append((n, c, tools_l or []))
                    except Exception:
                        pass
    except asyncio.TimeoutError:
        for task, name in tasks.items():
            if not task.done():
                task.cancel()
                logger.warning("Broadcast: {} cancelled (global timeout)", name)
                await engine._send(f"\u23f0 {name} timeout")

    # (auto-share logic is now inside _run_one's auto-wait cycle)

    # ── Round summary ──
    comm_count = len(mailbox.history)
    round_duration = _time.time() - _round_t0
    engine._save_round_summary(
        round_num=engine._round + 1,
        agents_responded=completed,
        comm_count=comm_count,
        duration=round_duration,
    )
    await engine._send(_d.broadcast_complete_msg(completed, total, comm_count))

    # Output chat chain summary
    # chain = _d.chat_chain_summary(mailbox.history, leader=leader_name)
    # if chain:
    #     await engine._send(chain)

    # Clean up queues (history preserved for synthesis & test harness)
    mailbox.clear()

    # Clear broadcast task registry on the engine
    if hasattr(engine, '_broadcast_tasks'):
        engine._broadcast_tasks.clear()

    # ── Restore original settings (session-scoped overrides) ──
    if _original_settings:
        for name, orig in _original_settings.items():
            cfg = engine.registry.get(name)
            if cfg and orig.get("tools"):
                cfg["tools"] = orig["tools"]
        logger.info("Broadcast: restored original agent settings")

    return [(name, content) for name, content, _ in results]

