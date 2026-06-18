"""Broadcast execution mode for group chat.

Runs all agents concurrently with out-of-order display.
Agents can communicate with each other via chatroom_send/wait tools.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.groupchat.orchestra.broadcast_context import BroadcastContext
from nanobot.groupchat.orchestra.broadcast_orchestrator import BroadcastOrchestrator
from nanobot.groupchat.orchestra.broadcast_status import AgentStatusTracker
from nanobot.groupchat.orchestra.mailbox import MailboxHub

# Re-exports for backward compatibility
__all__ = [
    "AgentStatusTracker",
    "BroadcastContext",
    "BroadcastOrchestrator",
    "broadcast_round",
]

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
    # Clear stale rank cache from previous round
    if hasattr(broadcast_round, "_agent_ranks_cache"):
        del broadcast_round._agent_ranks_cache
    if not agents:
        return []

    # Lazy-connect MCP servers before building tool registries
    if hasattr(engine, '_connect_mcp'):
        await engine._connect_mcp()

    import time as _time
    _round_t0 = _time.time()

    # ── Detect leader ──
    leader_name = engine._leader if hasattr(engine, '_leader') else None
    if leader_name and leader_name not in agents:
        leader_name = None

    # Wire leader info into mailbox for listener restriction feature
    mailbox.set_leader_name(leader_name or "")

    # Push agent ranks into mailbox for interrupt hierarchy
    ranks_map: dict[str, str] = {}
    for ag in agents:
        cfg = engine.registry.get(ag, {})
        if "rank" in cfg:
            ranks_map[ag] = str(cfg["rank"])
        else:
            ranks_map[ag] = "basic"
    mailbox.set_ranks(ranks_map, leader=leader_name or "")

    # ── Clear any leftover session tool overrides ──
    if hasattr(engine, "_session_tools_override"):
        engine._session_tools_override.clear()

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
    await engine._send(_d.broadcast_start_msg(list(agents), int(global_timeout), leader=leader_name, ranks=ranks_map))

    orch = BroadcastOrchestrator(agents, engine, mailbox)

    # ── Extract user question (for hint injection) ──
    user_question = ""
    for msg in reversed(engine._history):
        if msg.get("sender") in ("User", "user", "用户"):
            content = msg.get("content", "")
            if content.startswith("["):
                continue  # skip compressed summary blocks
            user_question = content[:300]
            break

    # ═══════════════════════════════════════════════════════════════
    # Agent Execution (broadcast) — leader runs as active agent
    # ═══════════════════════════════════════════════════════════════

    def _spawn_agent_task(name: str, idx: int) -> asyncio.Task:
        """Re-spawn a single agent task (used by ManageAgentTool.restart)."""
        task = asyncio.create_task(_run_one(name, idx))
        orch.tasks[task] = name
        orch.all_tasks.add(task)
        if hasattr(engine, '_broadcast_tasks'):
            engine._broadcast_tasks[name] = task
        return task

    await orch.setup_tools_and_pools(_spawn_agent_task)

    # ── Compute agent_ranks early (needed by BroadcastView and message_converter) ──
    from nanobot.groupchat.display.visibility import compute_agent_ranks
    if not hasattr(broadcast_round, "_agent_ranks_cache"):
        broadcast_round._agent_ranks_cache = compute_agent_ranks(list(agents), engine.registry, leader_name)
    agent_ranks = broadcast_round._agent_ranks_cache

    from nanobot.groupchat.display.broadcast_view import BroadcastView
    view = BroadcastView(engine, orch.tracker, mailbox, orch.pool, orch.search_pool, list(agents), leader_name, agent_ranks=agent_ranks)

    # Map back to local variables to keep downstream code unmodified for now
    exec_agents = orch.exec_agents
    non_leader_agents = orch.non_leader_agents
    total = orch.total
    gc_settings = orch.gc_settings
    pool = orch.pool
    tracker = orch.tracker
    search_pool = orch.search_pool
    leader_gate = orch.leader_gate
    agent_tool_registries = orch.agent_tool_registries
    _search_cache = orch._search_cache
    leader_end_event = orch.leader_end_event
    _leader_agent_tasks = orch._leader_agent_tasks
    tasks = orch.tasks
    all_tasks = orch.all_tasks

    # ── Run each agent as a concurrent task ──


    from nanobot.groupchat.orchestra.broadcast_agent import AgentTurnContext, run_agent_turn

    turn_ctx = AgentTurnContext(
        engine=engine,
        agents=agents,
        leader_name=leader_name,
        non_leader_agents=non_leader_agents,
        total=total,
        agent_ranks=agent_ranks,
        user_question=user_question,
        mailbox=mailbox,
        pool=pool,
        tracker=tracker,
        search_pool=search_pool,
        agent_tool_registries=agent_tool_registries,
        gc_settings=gc_settings,
        view=view,
        leader_end_event=leader_end_event,
        exec_agents=exec_agents,
        ranks_map=ranks_map,
    )

    async def _run_one(name: str, agent_idx: int):
        return await run_agent_turn(name, agent_idx, turn_ctx)

    # ── Launch all agents (including leader) concurrently ──
    for name in exec_agents:
        mailbox.create(name)
    mailbox.start_round(active_agents=list(exec_agents))

    # populate tasks dict previously initialized
    for idx, name in enumerate(exec_agents):
        task = asyncio.create_task(_run_one(name, idx))
        tasks[task] = name
        all_tasks.add(task)

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

    # Auxiliary tasks that must be cleaned up even on CancelledError.
    # Initialised to None so the finally block is safe if creation fails.
    user_task: asyncio.Task | None = None
    join_task: asyncio.Task | None = None
    leader_end_sentinel: asyncio.Task | None = None
    _user_listener_running = True
    _join_listener_running = True

    try:
        # ── User interjection listener ──

        async def _user_listener() -> None:
            while _user_listener_running:
                try:
                    msg = await asyncio.wait_for(engine._input_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg == "__SUMMARY__":
                    continue

                all_agent_names = list(mailbox.agent_names)
                await pool.allocate_user(all_agent_names)

                mailbox.create("用户")
                mailbox.send("用户", ["All"], msg)
                # Interrupt any agents currently inside tool_loop so they pick
                # up the user message at the next safe checkpoint rather than
                # waiting for their current tool batch to finish.
                _interrupted = mailbox.interrupt_busy_agents("用户")
                engine._add_message("用户", msg)
                await engine._send(
                    f"── User ──\n{msg}\n"
                    f"  {pool.status()}"
                )
                logger.info("Broadcast: user interjected: {} ({} agent(s) interrupted)", msg[:60], _interrupted)


        user_task = asyncio.create_task(_user_listener())

        # ── Mid-round agent join listener ──
        # Drains engine._pending_join_queue so agents added via /add during
        # an active round are spawned immediately rather than waiting for next round.

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
                from nanobot.tools.registry import ToolRegistry
                from nanobot.groupchat.orchestra.tools.chatroom_tools import (
                    ChatroomSendTool, WaitTool, CachedSearchTool,
                    QuoteMessageTool, ListMessagesTool,
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
                new_reg.register(QuoteMessageTool(mailbox=mailbox))
                new_reg.register(ListMessagesTool(mailbox=mailbox))
                new_cfg = engine.registry.get(new_name, {})
                new_session_cfg = None
                if hasattr(engine, "_session_tools_override") and new_name in engine._session_tools_override:
                    new_session_cfg = engine._session_tools_override[new_name]
                from nanobot.groupchat.tool_policy import (
                    forget_tool_enabled,
                    memory_palace_tool_enabled,
                )
                if forget_tool_enabled(new_cfg, session_override=new_session_cfg):
                    from nanobot.tools.forget import ForgetTool
                    new_reg.register(ForgetTool())
                if memory_palace_tool_enabled(new_cfg, session_override=new_session_cfg):
                    from nanobot.tools.memory_palace import MemoryPalaceTool
                    new_reg.register(MemoryPalaceTool())
                agent_tool_registries[new_name] = new_reg

                from nanobot.groupchat.display.visibility import per_agent_pool_capacities
                join_cap = per_agent_pool_capacities(
                    [new_name], engine.registry, leader_name,
                )[new_name]
                if pool:
                    pool.register_agent(new_name, join_cap)
                search_pool.register_agent(new_name, join_cap)

                mailbox.create(new_name)
                ranks_map: dict[str, str] = {}
                for ag in mailbox._active_agents | {new_name}:
                    cfg = engine.registry.get(ag, {})
                    ranks_map[ag] = str(cfg["rank"]) if "rank" in cfg else "basic"
                mailbox.set_ranks(ranks_map, leader=leader_name or "")
                mailbox._active_agents.add(new_name)
                idx = total
                total += 1
                tracker.add_agent(new_name)
                new_task = asyncio.create_task(_run_one(new_name, idx))
                tasks[new_task] = new_name
                all_tasks.add(new_task)
                engine._broadcast_tasks[new_name] = new_task
                await engine._send(
                    f"✅ {new_name} 加入当前讨论\n"
                    f"👥 当前成员: {', '.join(mailbox._active_agents)}"
                )
                # Notify leader so it can assign tasks to the new agent
                if leader_name and leader_name != new_name:
                    new_tools = new_cfg.get("tools", {})
                    if isinstance(new_tools, dict):
                        tool_list = [k for k, v in new_tools.items() if v]
                    else:
                        tool_list = list(engine.TOOL_NAMES) if new_cfg.get("tools_enabled", False) else []
                    mailbox.send(
                        "系统", [leader_name],
                        f"[新成员加入] {new_name} 已加入讨论。"
                        f"工具: {', '.join(tool_list) if tool_list else '无'}。"
                        f"请给 {new_name} 分配任务。",
                    )
                # Also send the new agent a kickstart message with context
                mailbox.send(
                    "系统", [new_name],
                    f"你刚刚加入了正在进行的群聊讨论。"
                    f"用户问题: {user_question}\n"
                    f"当前成员: {', '.join(mailbox._active_agents)}。"
                    f"{'Leader 是 ' + leader_name + '，等待 Leader 给你分配任务。' if leader_name and leader_name != new_name else '请开始工作。'}",
                )
                logger.info("Broadcast: dynamically spawned {} (idx={})", new_name, idx)

        join_task = asyncio.create_task(_join_listener())

        # Register auxiliary tasks on the engine so _stop_group_loop can cancel
        # them even when CancelledError short-circuits this function.
        if hasattr(engine, '_broadcast_tasks'):
            engine._broadcast_tasks['__user_listener'] = user_task
            engine._broadcast_tasks['__join_listener'] = join_task

        # Watch for leader end_discussion signal
        async def _watch_leader_end() -> None:
            await leader_end_event.wait()

        leader_end_sentinel = asyncio.create_task(_watch_leader_end())
        all_tasks.add(leader_end_sentinel)

        if hasattr(engine, '_broadcast_tasks'):
            engine._broadcast_tasks['__leader_sentinel'] = leader_end_sentinel

        while not all(t.done() for t in all_tasks):
            done_set, _ = await asyncio.wait(
                [t for t in all_tasks if not t.done()],
                timeout=global_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done_set:
                break

            for t in done_set:
                if t is leader_end_sentinel:
                    logger.info("Broadcast: leader ended discussion")
                    _end_reason = getattr(engine, '_leader_end_reason', '')
                    _reason_suffix = f"（{_end_reason}）" if _end_reason else ""
                    await engine._send(f"━━ Leader 结束讨论{_reason_suffix} — entering synthesis ━━")

                    # Graceful shutdown: give agents time to finish their current
                    # LLM generation cycle before force-cancelling. This prevents
                    # losing content that's already been generated but not yet returned.
                    GRACE_PERIOD = 15  # seconds

                    # Step 1: notify all non-leader agents to wrap up
                    for task_obj, task_name in tasks.items():
                        if not task_obj.done() and task_name != leader_name:
                            await tracker.set_state(task_name, "finishing", reason="leader ended")
                            mailbox.send("系统", [task_name],
                                "[系统通知] Leader 已结束讨论，请尽快完成当前输出并进入等待状态。")

                    # Step 2: wait for agents to naturally complete (up to grace period)
                    deadline = asyncio.get_event_loop().time() + GRACE_PERIOD
                    while asyncio.get_event_loop().time() < deadline:
                        still_running = [
                            t for t in tasks
                            if not t.done() and tasks[t] != leader_name
                        ]
                        if not still_running:
                            break
                        done_now, _ = await asyncio.wait(
                            still_running,
                            timeout=min(2.0, deadline - asyncio.get_event_loop().time()),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for dt in done_now:
                            if dt in tasks:
                                try:
                                    name, content, _, _ = dt.result()
                                    logger.info(
                                        "Broadcast: {} finished during grace period ({} chars)",
                                        name, len(content) if content else 0,
                                    )
                                except Exception:
                                    pass

                    # Step 3: force-cancel any stragglers
                    for task_obj, task_name in tasks.items():
                        if not task_obj.done() and task_name != leader_name:
                            await tracker.set_state(task_name, "cancelled", reason="leader ended")
                            task_obj.cancel()
                            logger.warning("Broadcast: {} force-cancelled after grace period", task_name)
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
            done_late, _ = await asyncio.wait(pending_cleanup, timeout=3)
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
    finally:
        # ── Guarantee cleanup of ALL sub-tasks, even on CancelledError ──
        # Without this, /stop causes CancelledError which bypasses the normal
        # cleanup path, leaving user_task/join_task/leader_end_sentinel as
        # orphaned tasks that steal messages from future sessions.
        _user_listener_running = False
        _join_listener_running = False
        _aux_to_cancel = []
        for aux_task in (user_task, join_task, leader_end_sentinel):
            if aux_task is not None and not aux_task.done():
                aux_task.cancel()
                _aux_to_cancel.append(aux_task)
                logger.debug("Broadcast: cancelled auxiliary task {}", aux_task.get_name())
        # Wait for auxiliary tasks to *actually* finish — without this, a cancelled
        # user_task can survive into the next session, reading from the new session's
        # _input_queue and calling pool.allocate_user() on the old pool (stale
        # capacity reference), which causes the "-4/30" thread bar display bug.
        if _aux_to_cancel:
            await asyncio.gather(*_aux_to_cancel, return_exceptions=True)
            logger.debug("Broadcast: all auxiliary tasks finished")
        # Remove auxiliary task entries from engine registry
        if hasattr(engine, '_broadcast_tasks'):
            for key in ('__user_listener', '__join_listener', '__leader_sentinel'):
                engine._broadcast_tasks.pop(key, None)

    # (auto-share logic is now inside _run_one's auto-wait cycle)

    # ── Finalize status dashboard ──
    await tracker.finalize()

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

    # ── Clear session tool overrides (set by ManageAgentTool) ──
    if hasattr(engine, "_session_tools_override"):
        engine._session_tools_override.clear()
        logger.info("Broadcast: cleared session tool overrides")

    return [(name, content) for name, content, _ in results]

