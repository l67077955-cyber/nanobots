"""Multi-agent concurrent round — main runtime path.

Orchestrates one round: setup (BroadcastOrchestrator) → per-agent cycles
→ History commits / WorkingMemory refresh. UI rendering is delegated to
``display.BroadcastView`` via callbacks.

Heavy helpers live in sibling modules:
- broadcast_orchestrator / broadcast_context; UI tracker in display.status_tracker
- events.trigger_realtime_interrupts
- agent cycle body: ``agent_cycle.run_agent_cycle`` (AgentCycleEnv)
"""

from __future__ import annotations

import asyncio
import copy
import random
import json as _json
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.groupchat.runtime.agent_runner import AgentRunner
from nanobot.groupchat.runtime.broadcast_context import BroadcastContext
from nanobot.groupchat.runtime.broadcast_orchestrator import BroadcastOrchestrator
from nanobot.groupchat.runtime.cycle_controller import (
    CycleAction,
    CycleContext,
    CycleController,
)
from nanobot.groupchat.runtime.events import trigger_realtime_interrupts
from nanobot.groupchat.runtime.mailbox import MailboxHub, ConversationPool
from nanobot.groupchat.runtime.turn_stack import TurnStack
from nanobot.groupchat.runtime.engine import log_request
from nanobot.groupchat.runtime.working_memory import WorkingMemory, commit_agent_turn
from nanobot.groupchat.runtime.tools.tool_chat import valid_agent_sampling
from nanobot.groupchat.context.component_manager import get_system_warning


def _valid_agent_sampling(agent_cfg: dict[str, Any]) -> dict[str, Any]:
    """Per-agent hyperparams → provider-safe sampling dict."""
    raw = agent_cfg.get("hyperparams")
    return valid_agent_sampling(raw if isinstance(raw, dict) else {})


async def _trigger_realtime_interrupts(
    sender: str,
    targets: list[str],
    mailbox: MailboxHub,
    engine: Any,
    leader_name: str | None,
) -> None:
    """Delegate to runtime.events (single implementation)."""
    await trigger_realtime_interrupts(
        sender=sender,
        targets=targets,
        mailbox=mailbox,
        engine=engine,
        leader_name=leader_name,
    )


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
    # Completely normalize to modern "basic/standard/advanced/expert" (legacy chess names only supported for loading old configs)
    LEGACY_TO_MODERN = {"pawn": "basic", "knight": "standard", "bishop": "advanced", "queen": "expert", "king": "expert"}
    ranks_map: dict[str, str] = {}
    for ag in agents:
        cfg = engine.registry.get(ag, {})
        raw = cfg.get("rank", "basic")
        normalized = LEGACY_TO_MODERN.get(raw, raw)
        ranks_map[ag] = normalized
        # Update in-memory so the entire round (banner, interrupts, pool, etc.) uses only modern names
        cfg["rank"] = normalized

        # Auto-migration on every broadcast start: rewrite old chess names in disk config to modern
        # This means after one restart, all agents will be fully on the new advanced/expert system
        if raw != normalized:
            try:
                cfg_path = Path.home() / ".nanobot" / "agents" / ag.lower() / "config.json"
                if cfg_path.exists():
                    disk_cfg = _json.loads(cfg_path.read_text())
                    disk_cfg["rank"] = normalized
                    cfg_path.write_text(_json.dumps(disk_cfg, indent=2, ensure_ascii=False))
                    logger.info("Auto-migrated rank for {} from legacy {} to modern {} (disk updated)", ag, raw, normalized)
            except Exception as e:
                logger.debug("Rank migration skipped for {}: {}", ag, e)
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
    _base_sampling = getattr(engine.provider, "sampling_params", {}) or {}
    _start_sampling: dict[str, dict[str, Any]] = {}
    for _agent in agents:
        _cfg = engine.registry.get(_agent, {})
        _effective = dict(_base_sampling) if isinstance(_base_sampling, dict) else {}
        _effective.update(_valid_agent_sampling(_cfg))
        _reasoning_effort = _effective.get("reasoning_effort") or _cfg.get("reasoning_effort")
        if _reasoning_effort:
            _effective["reasoning_effort"] = _reasoning_effort
        _start_sampling[_agent] = _effective
    await engine._send(_d.broadcast_start_msg(
        list(agents),
        int(global_timeout),
        leader=leader_name,
        ranks=ranks_map,
        sampling=_start_sampling,
    ))

    orch = BroadcastOrchestrator(agents, engine, mailbox)

    # ── Extract user question via context-layer façade ──
    from nanobot.groupchat.context.conversation import conversation_from_engine
    _conversation = conversation_from_engine(engine)
    user_question = _conversation.latest_user_content(max_len=300)

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

    # ── Compute agent_ranks early (needed by BroadcastView and build_for_groupchat visibility) ──
    from nanobot.groupchat.context.ranks import compute_agent_ranks
    if not hasattr(broadcast_round, "_agent_ranks_cache"):
        broadcast_round._agent_ranks_cache = compute_agent_ranks(list(agents), engine.registry, leader_name)
    agent_ranks = broadcast_round._agent_ranks_cache

    from nanobot.groupchat.display.broadcast_view import BroadcastView

    async def _on_chatroom_send_ok(sender: str, targets: list[str]) -> None:
        await _trigger_realtime_interrupts(
            sender=sender,
            targets=targets,
            mailbox=mailbox,
            engine=engine,
            leader_name=leader_name,
        )

    view = BroadcastView(
        engine,
        orch.tracker,
        mailbox,
        orch.pool,
        orch.search_pool,
        list(agents),
        leader_name,
        agent_ranks=agent_ranks,
        on_chatroom_send_ok=_on_chatroom_send_ok,
    )

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

    # ── Per-agent cycle (middle logic layer) ──
    from nanobot.groupchat.runtime.agent_cycle import AgentCycleEnv, run_agent_cycle

    _cycle_env = AgentCycleEnv(
        engine=engine,
        mailbox=mailbox,
        leader_name=leader_name,
        leader_end_event=leader_end_event,
        agent_ranks=agent_ranks,
        agent_tool_registries=agent_tool_registries,
        agents=list(agents),
        exec_agents=list(exec_agents),
        non_leader_agents=list(non_leader_agents),
        gc_settings=gc_settings,
        pool=pool,
        search_pool=search_pool,
        tracker=tracker,
        view=view,
        total=total,
        user_question=user_question or "",
        trigger_realtime_interrupts=_trigger_realtime_interrupts,
        valid_agent_sampling=_valid_agent_sampling,
        base_sampling=dict(_base_sampling) if isinstance(_base_sampling, dict) else {},
    )

    async def _run_one(
        name: str,
        agent_idx: int,
    ) -> tuple[str, str | None, list[str], dict]:
        return await run_agent_cycle(_cycle_env, name, agent_idx)

    # ── Launch all agents (including leader) concurrently ──
    for name in exec_agents:
        mailbox.create(name)
    mailbox.start_round(active_agents=list(exec_agents))

    # TurnStack: the turn-level seam for this round (interject / cancel_all).
    # Registered on the engine so _stop_group_loop and future turn-level code
    # reach through the port instead of mailbox/pool internals.
    turn_stack = TurnStack(engine, mailbox, pool, exec_agents)
    engine._turn_stack = turn_stack

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

                # Delegate to the TurnStack seam: it force-allocates pool slots,
                # broadcasts to all agents, interrupts busy ones, records +
                # displays the message. Returns False (after requeuing) when the
                # round is winding down — then we exit so run_loop picks it up
                # as a fresh round (the "stuck until next user message" guard).
                if not await turn_stack.interject(msg):
                    return


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
                from nanobot.groupchat.runtime.tools.chatroom_tools import (
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
                agent_tool_registries[new_name] = new_reg
                # Register with search pool (initialize credits for new agent)
                with search_pool._lock:
                    search_pool._agents.append(new_name)
                    search_pool._credits[new_name] = search_pool._initial
                    search_pool._searches[new_name] = 0
                    search_pool._outputs[new_name] = 0
                # Register with conversation pool to prevent slot leaks
                if pool and new_name not in pool._pending:
                    pool._pending[new_name] = []
                # Register with mailbox
                mailbox.create(new_name)
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
                    new_cfg = engine.registry.get(new_name, {})
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
                                    name, content, tools_used_list, *_ = dt.result()
                                    completed += 1
                                    results.append((name, content, tools_used_list or []))
                                    logger.info(
                                        "Broadcast: {} finished during grace period ({} chars) — counted {}/{}",
                                        name, len(content) if content else 0,
                                        completed, total,
                                    )
                                except Exception:
                                    logger.warning(
                                        "Broadcast: agent task error during grace period: {}",
                                        dt,
                                    )

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
        # Drop the round's TurnStack reference (its tasks are being cancelled).
        engine._turn_stack = None
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
    comm_count = len(mailbox.round_log)
    round_duration = _time.time() - _round_t0
    engine._save_round_summary(
        round_num=engine._round + 1,
        agents_responded=completed,
        comm_count=comm_count,
        duration=round_duration,
    )
    await engine._send(_d.broadcast_complete_msg(completed, total, comm_count))

    # Output chat chain summary
    # chain = _d.chat_chain_summary(mailbox.round_log, leader=leader_name)
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

# Optional clearer alias (same function)
run_round = broadcast_round
