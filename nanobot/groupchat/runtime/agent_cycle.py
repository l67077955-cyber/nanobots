"""Per-agent cycle body — middle logic layer (groupchat.runtime).

Extracted from nested ``broadcast_round._run_one`` so the round
orchestrator (setup / launch / gather) stays separate from the
long per-agent ``while True`` cycle loop.

Layering
--------
- **runtime** (this module): when to run, interrupt, tool_loop, commit
- **context**: prompt build / History via ``engine.history`` + ConversationContext
- **display**: StreamingDisplay / BroadcastView called from here via callbacks

Free variables that used to close over ``broadcast_round`` now live on
:class:`AgentCycleEnv`.
"""

from __future__ import annotations

import asyncio
import copy
import json as _json
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.groupchat.runtime.agent_runner import AgentRunner
from nanobot.groupchat.runtime.cycle_controller import (
    CycleAction,
    CycleContext,
    CycleController,
)
from nanobot.groupchat.runtime.mailbox import ConversationPool, MailboxHub
from nanobot.groupchat.runtime.collab_bus import CollabBus, deliver
from nanobot.groupchat.runtime.working_memory import WorkingMemory, commit_agent_turn
from nanobot.groupchat.context.component_manager import get_system_warning


@dataclass
class AgentCycleEnv:
    """Shared round state for one concurrent agent cycle.

    Built once per ``broadcast_round``; each agent task receives the same
    env instance (plus its own ``name`` / ``agent_idx``).
    """

    engine: Any
    mailbox: MailboxHub
    leader_name: str | None
    leader_end_event: Any
    agent_ranks: dict[str, int]
    agent_tool_registries: dict[str, Any]
    agents: list[str]
    exec_agents: list[str]
    non_leader_agents: list[str]
    gc_settings: dict[str, Any]
    pool: ConversationPool | None
    search_pool: Any
    tracker: Any
    view: Any
    total: int
    user_question: str
    trigger_realtime_interrupts: Callable[..., Awaitable[None]]
    valid_agent_sampling: Callable[[dict[str, Any]], dict[str, Any]]
    base_sampling: dict[str, Any]


async def run_agent_cycle(
    env: AgentCycleEnv,
    name: str,
    agent_idx: int,
) -> tuple[str, str | None, list[str], dict]:
    """Run a single agent with streaming display."""
    # ── Round environment (was nested closure in broadcast_round) ──
    engine = env.engine
    mailbox = env.mailbox
    bus: CollabBus = mailbox
    leader_name = env.leader_name
    leader_end_event = env.leader_end_event
    agent_ranks = env.agent_ranks
    agent_tool_registries = env.agent_tool_registries
    agents = env.agents
    exec_agents = env.exec_agents
    non_leader_agents = env.non_leader_agents
    gc_settings = env.gc_settings
    pool = env.pool
    search_pool = env.search_pool
    tracker = env.tracker
    view = env.view
    total = env.total
    user_question = env.user_question
    _trigger_realtime_interrupts = env.trigger_realtime_interrupts
    _valid_agent_sampling = env.valid_agent_sampling
    _base_sampling = env.base_sampling

    import time as _t
    _cycle_t0 = _t.time()

    if name not in engine.registry:
        return (name, None, [], {})

    agent_cfg = engine.registry[name]
    model = agent_cfg["model"]  # initial; re-read each cycle below
    model_short = model.split("/")[-1]

    # ── Compute teammates list (must be before _build_agent_prompt call) ──
    teammates = [a for a in agents if a != name]

    # agent_ranks already computed above (before BroadcastView creation)
    # In broadcast mode, all agents share the full history (relevant_agents=None).
    # Each agent sees user messages, system messages, and ALL teammates' final replies.
    # Tool call logs are appended as text inside each agent's message, so teammates
    # can read a concise summary of what was done without the full tool protocol overhead.
    # Rank-based isolation: agents only see tool calls from agents with rank <= their own.
    def _build_prompt_snapshot() -> list[dict[str, Any]]:
        return engine._build_agent_prompt(
            name,
            relevant_agents=None,
            agent_ranks=agent_ranks,
            agent_idx=agent_idx,
            total=total,
            teammates=teammates,
            user_question=user_question,
        )

    # Working memory: ephemeral LLM session. Shared transcript is engine.history.
    wm = WorkingMemory(messages=_build_prompt_snapshot())
    messages = wm.messages

    is_leader = (name == leader_name)
    _leader_ended_discussion = False
    # Load from override system (editable via /prompt), fallback to default
    # Removed stale prompt_overrides.json lookup; .md files are the source of truth.

    if is_leader:
        # ── Leader prompt: active orchestrator ──
        agent_caps = []
        for a in non_leader_agents:
            on = engine.get_agent_enabled_tool_names(a)
            agent_caps.append(f"  {a}: {', '.join(on) if on else '(无工具)'}")

        # Determine Leader's own base tools
        leader_on = engine.get_agent_enabled_tool_names(leader_name)
        leader_base_tools_str = f"（{', '.join(leader_on)}）" if leader_on else "（无基础工具）"

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
            f"- 你也拥有自己的基础工具{leader_base_tools_str}，可以自己做部分工作\n\n"
            f"## 🧠 记忆宫殿（所有 Agent 共享）\n"
            f"memory_palace 工具在本轮结束后仍然保留，下次启动自动加载。\n"
            f"- memory_palace(action='store', content=..., wing=..., hall=..., room=...)\n"
            f"    存入记忆。wing=大类（如'项目知识'），hall=子类（如'2026-04'），room=具体槽位\n"
            f"- memory_palace(action='search', query=..., top_k=5)\n"
            f"    关键词检索所有记忆，返回最相关的 top_k 条\n"
            f"- memory_palace(action='list')\n"
            f"    查看当前宫殿结构（Wing/Hall/Room 及记忆数量）\n"
            f"- memory_palace(action='delete', wing=..., hall=..., room=...)\n"
            f"    删除指定路径的记忆\n\n"
            f"每个 agent 有独立的搜索额度，详见下方的 [本轮状态汇总]。\n"
            f"你可以用 transfer_credits 把闲置额度划拨给需要的队友。\n\n"
            f"## 工作流程\n"
            f"1. 先用 memory_palace(action='search') 检索是否有相关历史记忆\n"
            f"2. 分析问题，决定如何分工\n"
            f"3. 用 chatroom_send 给队友分配具体任务（写清楚要做什么）\n"
            f"   ⚠️ 只分配队友有工具能力完成的任务！无 web_search 的队友不要让他搜索\n"
            f"4. 用 wait() 等待队友回复结果\n"
            f"5. 根据结果：追加任务 / 纠正方向 / 自己补充搜索\n"
            f"6. 信息充分后，先完成以下两步，再调用 end_discussion()：\n"
            f"   a. 输出结构化最终总结（必须包含以下部分，禁止省略）：\n"
            f"      ## 结论\n"
            f"      （直接回答用户问题的核心结论，1-3句话）\n\n"
            f"      ## 关键发现\n"
            f"      （讨论中确认的事实、数据、来源，用列表形式）\n\n"
            f"      ## 备注\n"
            f"      （可选：分歧说明、局限、后续建议）\n\n"
            f"   b. 用 memory_palace(action='store') 将关键结论写入记忆宫殿\n"
            f"      示例: memory_palace(action='store', content='用户偏好：...', wing='用户', hall='偏好', room='main')\n"
            f"7. 完成记忆存入后，调用 end_discussion() 结束任务\n\n"
            f"## 关键规则\n"
            f"- 发现队友空转或无法完成任务时：果断 end_discussion\n"
            f"- 可以一次给多个队友同时发任务（并行工作）\n"
            f"- ⚠️ 如果你打算自己做搜索/验证，必须先完成工具调用，再调用 end_discussion。\n"
            f"  end_discussion 一旦触发无法撤销，之后再说'我来搜索'只是文字，不会执行。\n"
            f"- ⚠️ 原假设被否证时，不要立即结束。应转向：'那么最近的可验证链条是什么？'\n"
            f"  继续搜索直到能给出正面结论（即使度数更高），而不是仅报告'不成立'。\n"
            f"- ⚠️ 禁止在未存记忆的情况下调用 end_discussion。存记忆 → end_discussion 是强制顺序。\n"
        )
        wm.insert_before_last({
            "role": "system",
            "content": leader_hint,
        })
        messages = wm.messages
    else:
        # ── Non-leader: broadcast_hint already expanded by build_agent_prompt ──
        pass

        # If there's a leader, tell non-leader agents to expect instructions
        if leader_name:
            wm.insert_before_last({
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
            messages = wm.messages

    # ── Inject agent permissions context (Placeholder) ──
    wm.insert_before_last({
        "role": "system",
        "content": "[团队工具权限及搜索额度见消息末尾 [本轮状态汇总]]",
    })
    messages = wm.messages

    # The volatile state message is always the last one (added by PromptBuilder)
    volatile_msg_idx = wm.volatile_index

    # ── Edit-in-place display (broadcast mode) ──
    # Each tool call gets one message (🟡), then edited with result (🟢/🔴).
    _tool_lines: list[str] = []
    _pending_tool_msgs: dict[str, tuple[int | None, str]] = {}  # tool_call_id → (msg_id, original_text)
    # Shared state between _on_tool_start and _on_tool_result for chatroom_send args
    _last_chatroom_send_to: list[str] = []

    # Stream header — sourced from the display layer (single source of truth)
    # so the symbol stays consistent with display.agent_header (▍), matching
    # the stable broadcast UI. Previously hardcoded here as `◍`, which
    # diverged from display.agent_header's `▍`.
    _stream_header = _d.agent_header(
        name, leader=leader_name, idx=agent_idx + 1, total=total, mode="broadcast",
    )

    # Send initial status
    await engine._send(_d.thinking_msg(name, model_short, leader=leader_name, idx=agent_idx + 1, total=total))
    async def _on_tool_start(tool_name: str, args: dict, **_kw) -> None:
        tool_call_id = _kw.get("tool_call_id", "")
        if not isinstance(args, dict):
            args = {}
        await view.on_tool_start(name, tool_name, args, tool_call_id, _cycle_t0, _cycle_usage)

    async def _on_tool_result(tool_name: str, tool_call_id: str, result: str) -> None:
        await view.on_tool_result(name, tool_name, tool_call_id, result)

    # ── Streaming display ───────────────────────────────────────────
    # Reuse the display layer's StreamingDisplay (same as direct_chat):
    # it throttles edits to EDIT_INTERVAL=0.8s (avoiding Telegram rate
    # limits that caused the previous per-token edit stalls), handles
    # tool-call resets (abandon mid-stream message → new one below tools),
    # and finalizes by editing the same message (no duplicate send).
    # The previous hand-rolled _on_content_delta edited on every delta
    # with no throttle + re-sent the content at cycle end → lag + dupes.
    from nanobot.groupchat.display.streaming import StreamingDisplay
    _stream: StreamingDisplay | None = None  # created fresh each cycle below

    # ── Run tool-loop + auto-wait cycle ──
    # After tool_loop finishes, agent automatically enters wait().
    # If a teammate message arrives, inject it and re-run tool_loop.
    # Only exits when cancelled by leader end_discussion, /stop, or on error.
    from nanobot.groupchat.runtime.tools.tool_loop import tool_loop

    # Load configurable result_max_chars for broadcast mode
    try:
        from nanobot.groupchat.context.history_settings import broadcast_result_max_chars
        _broadcast_result_max = broadcast_result_max_chars()
    except Exception:
        _broadcast_result_max = 20_000

    reg = agent_tool_registries[name]
    # Always include chatroom tools; memory_palace only if registered (i.e. enabled)
    broadcast_tool_names = ["chatroom_send", "wait"]
    if reg.get("memory_palace") is not None:
        broadcast_tool_names.append("memory_palace")
    if is_leader:
        broadcast_tool_names.extend(["manage_agent", "end_discussion", "transfer_credits", "clear_context"])
    broadcast_defs = [
        t.to_schema() for t in [
            reg.get(tn) for tn in broadcast_tool_names
        ]
        if t is not None
    ]

    all_tools_used: list[str] = []
    total_iterations = 0
    total_latency = 0.0
    cycle = 0
    content = ""  # last cycle's text output
    agent_max_iters = 12 if is_leader else 8
    max_cycles = 30 if is_leader else 20  # hard cap to prevent runaway agents
    _substantive_tools = {"web_search", "web_fetch", "exec", "read_file", "write_file"}
    # Separate system-prompt messages (stable prefix) from conversation messages
    # so we can prune conversation turns without touching the system prompt.
    _sys_msg_count = wm.sys_msg_count

    # ── AgentRunner: per-agent runtime facade (cancel signal + state) ──
    # The runner wraps this agent's interrupt event + task; it is the
    # canonical handle new code uses to interrupt/cancel/inspect the agent.
    # State still lives on mailbox/engine (delegating facade, no state moved
    # yet — see docs/groupchat-coupling-fix.md Step 0.5). Same event object
    # as the old mailbox.get_interrupt_event(name) call, so zero behaviour
    # change; tool_loop and wait() race runner.interrupt_event.
    _runner = AgentRunner(name, mailbox, lambda: engine._broadcast_tasks.get(name))
    engine._runners[name] = _runner
    _interrupt_event = _runner.interrupt_event

    # ── CycleController: per-agent cycle-loop decision oracle (Step 3b) ──
    # Pure decision oracle — bodies stay inline, only branch conditions move
    # here. Shadow mode: oracle runs in parallel with existing logic and
    # assertions verify consistency before we switch.
    _cycle_ctrl = CycleController(name)
    # Tracks how many timeout-recovery attempts this agent has made.
    # Hard cap at 1 to prevent recovery loops.
    _timeout_recovery_count = 0
    # Tracks consecutive LLM errors to prevent rapid-fire error loops.
    # After MAX_CONSECUTIVE_ERRORS, the agent exits instead of continuing.
    _consecutive_error_count = 0
    MAX_CONSECUTIVE_ERRORS = 3

    try:
        while True:
            # ── Shadow mode: CycleController verification (Step 3b) ──
            # Build CycleContext and ask the oracle what it would do.
            # Log mismatches prominently but don't crash production.
            _shadow_ctx = CycleContext(
                agent_name=name,
                is_leader=is_leader,
                cycle=cycle,
                max_cycles=max_cycles,
                total_agents=total,
                engine_running=engine._running,
                discussion_ended=(mailbox.is_discussion_ended() if mailbox else False),
                leader_ended_discussion=_leader_ended_discussion,
                leader_end_event_set=leader_end_event.is_set() if leader_end_event else False,
                finish_reason="",  # not relevant for cycle_gate
                content=content,
                tools_used=(),
                substantive_tools=_substantive_tools,
                timeout_recovery_count=_timeout_recovery_count,
                consecutive_error_count=_consecutive_error_count,
                max_consecutive_errors=MAX_CONSECUTIVE_ERRORS,
            )
            _shadow_gate = _cycle_ctrl.decide_cycle_gate(_shadow_ctx)
            # Check oracle agrees with existing logic
            _gate_should_exit_max = cycle >= max_cycles
            _gate_should_exit_stop = (not engine._running or (mailbox and mailbox.is_discussion_ended())) and not (is_leader and _leader_ended_discussion)
            _gate_ok = (
                (_gate_should_exit_max and _shadow_gate.action is CycleAction.EXIT_MAX_CYCLES_FORCE_SYNTHESIS) or
                (_gate_should_exit_stop and _shadow_gate.action is CycleAction.EXIT_STOPPED_OR_ENDED) or
                (not _gate_should_exit_max and not _gate_should_exit_stop and _shadow_gate.action is CycleAction.PROCEED_TO_CYCLE)
            )
            if not _gate_ok:
                logger.error(
                    "SHADOW MISMATCH @ cycle_gate: cycle={} max={} running={} disc_ended={} is_leader={} leader_ended={} oracle={} existing_max={} existing_stop={}",
                    cycle, max_cycles, engine._running, mailbox.is_discussion_ended() if mailbox else False,
                    is_leader, _leader_ended_discussion, _shadow_gate.action, _gate_should_exit_max, _gate_should_exit_stop,
                )
            # ── End shadow verification ──

            # Hard cycle cap — prevent runaway agents from draining resources
            if cycle >= max_cycles:
                logger.warning(
                    "Broadcast: {} hit max_cycles={}, forcing exit", name, max_cycles
                )
                if not content:
                    messages = wm.refresh(
                        _build_prompt_snapshot,
                        trailing=[{
                            "role": "system",
                            "content": "[已达到最大轮次限制，请立即输出最终总结，禁止再调用工具。]",
                        }],
                    )
                    _sys_msg_count = wm.sys_msg_count
                    volatile_msg_idx = wm.volatile_index
                    try:
                        _r = await tool_loop(
                            provider=engine.provider,
                            messages=messages,
                            tool_registry=reg,
                            model=model,
                            max_tokens=engine.config.max_tokens,
                            max_iterations=1,
                            tool_defs=None,
                        )
                        content = _r.content or ""
                    except Exception:
                        pass
                break
            # Respect /stop — exit immediately if engine is no longer running.
            # Exception: leader called end_discussion but hasn't produced valid
            # synthesis yet — allow the cycle loop to continue so the leader
            # can retry (guards at line ~1125/1140 force a text-producing cycle).
            if (not engine._running or (mailbox and getattr(mailbox, "is_discussion_ended", lambda: False)())) and not (is_leader and _leader_ended_discussion):
                logger.info("Broadcast: {} exiting — engine stopped or discussion ended", name)
                break
            cycle += 1
            # Re-read model from registry each cycle so mid-round changes take effect
            _live_cfg = engine.registry.get(name, agent_cfg)
            model = _live_cfg.get("model", model)

            # Fresh StreamingDisplay per cycle (mirrors direct_chat): a new
            # LLM call starts a new streaming message. Reusing the same
            # instance across cycles would edit the previous cycle's message.
            _stream = StreamingDisplay(_stream_header, engine._send_and_get_id_fn, engine._edit_fn)
            _stream_on = getattr(engine, "stream_replies", True) and _stream.enabled
            if _stream_on:
                engine.register_active_stream(_stream)
            _on_content_delta = _stream.on_delta if _stream_on else None
            _on_content_reset = _stream.on_reset if _stream_on else None

            # ── Determine tool definitions for this cycle ──
            # Rebuild each cycle so mid-round set_tools changes take effect
            tool_defs = engine._get_agent_tools(_live_cfg, reg, agent_name=name)
            if tool_defs:
                existing_names = {d["function"]["name"] for d in tool_defs}
                for bd in broadcast_defs:
                    if bd["function"]["name"] not in existing_names:
                        tool_defs.append(bd)
            else:
                tool_defs = list(broadcast_defs)

            # ── Update volatile status summary (Permissions + Credits) ──
            perm_lines = []
            for a in exec_agents:
                on = engine.get_agent_enabled_tool_names(a)
                extra = " ← 你" if a == name else (" 👑Leader" if a == leader_name else "")
                perm_lines.append(f"  {a}: {', '.join(on) if on else '(无工具)'}{extra}")

            status_summary = (
                f"\n\n### [本轮状态汇总]\n"
                f"**搜索额度**: {search_pool.status()}\n"
                f"**工具权限**:\n" + "\n".join(perm_lines) + "\n\n"
                "注意：没有 web_search/web_fetch 权限时，也禁止用 exec 执行 curl/wget 等网络命令。\n"
                "如需搜索，请通过 chatroom_send 请求有搜索权限的队友帮忙。"
            )

            # Append to the volatile user message (the last message)
            # This ensures the system messages remain stable and cacheable.
            orig_volatile = messages[volatile_msg_idx]["content"]
            if "### [本轮状态汇总]" in orig_volatile:
                # Strip previous summary if retrying/looping
                orig_volatile = orig_volatile.split("### [本轮状态汇总]")[0].strip()

            messages[volatile_msg_idx]["content"] = orig_volatile + status_summary

            _cycle_t0 = _t.time()
            _cycle_usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}

            async def _on_iter_usage(usage: dict) -> None:
                for k in ("prompt", "completion", "total"):
                    _cycle_usage[k] += usage.get(k, usage.get(f"{k}_tokens", 0))

            # ── Pre-tool_loop size guard ──
            # After wait/interrupt/nudge re-entry all refresh from History,
            # working memory should stay bounded. If a path still grew it
            # (legacy injects / tool_loop multi-iter), rebuild from History
            # rather than AI-pruning a private list that can desync.
            _conv_keep_turns = gc_settings.get("conv_keep_turns", 3)
            _max_conv_msgs = _sys_msg_count + (_conv_keep_turns * 3) + 6
            if len(messages) > _max_conv_msgs:
                logger.info(
                    "Broadcast: {} working memory oversized ({} > {}), refreshing from History",
                    name, len(messages), _max_conv_msgs,
                )
                messages = wm.refresh(_build_prompt_snapshot)
                _sys_msg_count = wm.sys_msg_count
                volatile_msg_idx = wm.volatile_index

            # Per-agent hyperparams: pass per-call sampling so concurrent
            # broadcast agents do not mutate shared provider state.
            _agent_sampling = _valid_agent_sampling(_live_cfg)
            _base_sampling = getattr(engine.provider, "sampling_params", {}) or {}
            _effective_sampling = dict(_base_sampling) if isinstance(_base_sampling, dict) else {}
            if _agent_sampling:
                _effective_sampling.update(_agent_sampling)

            _reasoning_effort = (
                (_effective_sampling or {}).get("reasoning_effort")
                or _live_cfg.get("reasoning_effort")
                or None
            )

            # Mark agent busy so incoming messages can trigger interrupt
            _runner.begin_cycle()
            try:
                result = await tool_loop(
                    provider=engine.provider,
                    messages=messages,
                    tool_registry=reg,
                    model=model,
                    max_tokens=engine.config.max_tokens,
                    max_iterations=agent_max_iters,
                    tool_defs=tool_defs if tool_defs else None,
                    reasoning_effort=_reasoning_effort,
                    sampling_params=_effective_sampling,
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
                    on_content_delta=_on_content_delta,
                    on_content_reset=_on_content_reset,
                    clean_response=lambda c: engine._clean_response(c, name),
                    result_max_chars=_broadcast_result_max,
                    call_timeout=float(gc_settings.get("leader_call_timeout" if is_leader else "call_timeout", 90)) or None,
                    interrupt_event=_interrupt_event,
                )
            finally:
                # Always mark idle when tool_loop exits (interrupt, stop, normal, error)
                _runner.end_cycle()


            # Flush any remaining buffered search lines

            content = result.content or ""
            is_error = result.finish_reason == "error"
            is_timeout = result.finish_reason == "timeout"
            is_interrupted = result.finish_reason == "interrupted"
            latency = result.latency
            total_latency += latency
            total_iterations += result.iterations
            all_tools_used.extend(result.tools_used or [])

            if is_leader and "end_discussion" in (result.tools_used or []) and not engine._running:
                _leader_ended_discussion = True

            # ── Shadow: error_recovery decision ──
            _shadow_err_ctx = CycleContext(
                agent_name=name,
                is_leader=is_leader,
                cycle=cycle,
                max_cycles=max_cycles,
                total_agents=total,
                engine_running=engine._running,
                discussion_ended=(mailbox.is_discussion_ended() if mailbox else False),
                leader_ended_discussion=_leader_ended_discussion,
                leader_end_event_set=leader_end_event.is_set() if leader_end_event else False,
                finish_reason=result.finish_reason,
                content=content,
                tools_used=tuple(result.tools_used or []),
                substantive_tools=_substantive_tools,
                timeout_recovery_count=_timeout_recovery_count,
                consecutive_error_count=_consecutive_error_count,
                max_consecutive_errors=MAX_CONSECUTIVE_ERRORS,
            )
            _shadow_err = _cycle_ctrl.decide_error_recovery(_shadow_err_ctx)
            # Check oracle vs existing logic (nested conditions)
            _err_should_first_timeout = is_timeout and _timeout_recovery_count == 0
            _err_should_repeat_fallthrough = is_timeout and _timeout_recovery_count != 0
            _err_should_max_break = is_error and _consecutive_error_count >= MAX_CONSECUTIVE_ERRORS
            _err_should_placeholder_continue = is_error and _consecutive_error_count < MAX_CONSECUTIVE_ERRORS
            _err_no_recovery = not (is_error or is_timeout)
            _err_ok = (
                (_err_should_first_timeout and _shadow_err.action is CycleAction.TIMEOUT_FIRST_RETRY) or
                (_err_should_repeat_fallthrough and _shadow_err.action is CycleAction.TIMEOUT_REPEATED_FALLTHROUGH) or
                (_err_should_max_break and _shadow_err.action is CycleAction.ERROR_MAX_BREAK) or
                (_err_should_placeholder_continue and _shadow_err.action is CycleAction.ERROR_PLACEHOLDER_CONTINUE) or
                (_err_no_recovery and _shadow_err.action is CycleAction.NO_ERROR_RECOVERY)
            )
            if not _err_ok:
                logger.error(
                    "SHADOW MISMATCH @ error_recovery: finish={} timeout_cnt={} err_cnt={} oracle={} is_timeout={} is_error={}",
                    result.finish_reason, _timeout_recovery_count, _consecutive_error_count,
                    _shadow_err.action, is_timeout, is_error,
                )
            # ── End shadow: error_recovery ──

            if is_error or is_timeout:
                if is_timeout:
                    _base_timeout = gc_settings.get(
                        "leader_call_timeout" if is_leader else "call_timeout",
                        90,
                    )
                    err_short = f"LLM 超时 ({_base_timeout}s)"

                    # ── Clean retry on first timeout ──
                    # Re-use the same messages context (no injection) so history
                    # stays clean. Run one short no-tool call to get at least a
                    # brief output rather than abandoning the turn entirely.
                    if _timeout_recovery_count == 0:
                        _timeout_recovery_count += 1
                        await tracker.set_state(name, "thinking", detail="retry...")
                        await engine._send(f"⏰ {name} 超时，重试中...")
                        logger.warning(
                            "Broadcast: {} LLM timeout ({:.1f}s), retrying once (no tools)",
                            name, latency,
                        )
                        try:
                            _r = await tool_loop(
                                provider=engine.provider,
                                messages=messages,          # unchanged — no injection
                                tool_registry=reg,
                                model=model,
                                max_tokens=600,             # short answer only
                                max_iterations=1,
                                tool_defs=None,             # text-only, no tools
                                call_timeout=60.0,          # hard cap for retry
                            )
                            if _r.content:
                                content = _r.content
                                total_latency += _r.latency
                                commit_agent_turn(engine, name, content)
                                search_pool.on_output(name)
                                deliver(bus, name, ["All"], content[:300])
                                await engine._send(
                                    _d.chatroom_send_msg(
                                        name, "重试输出", content, max_len=1000, leader=leader_name
                                    )
                                )
                                logger.info(
                                    "Broadcast: {} retry succeeded ({:.1f}s): {}",
                                    name, _r.latency, content[:80],
                                )
                                _timeout_recovery_count = 0
                                continue  # back to auto-wait

                        except Exception as _rec_exc:
                            logger.warning("Broadcast: {} recovery also failed: {}", name, _rec_exc)

                        # ── Recovery failed — send placeholder and stay alive ──
                        # Instead of hard-exiting, pretend the agent produced a
                        # brief status message so downstream flow continues.
                        _placeholder = (
                            f"⏳ [{name}] 当前模型响应超时，我仍在线。"
                            f"等待队友消息后将继续工作。"
                        )
                        content = _placeholder
                        commit_agent_turn(engine, name, _placeholder)
                        deliver(bus, name, ["All"], _placeholder)
                        await engine._send(
                            _d.chatroom_send_msg(
                                name, "超时占位", _placeholder, max_len=1000, leader=leader_name
                            )
                            )
                        await tracker.set_state(name, "waiting", detail="timeout recovery")
                        logger.warning(
                            "Broadcast: {} timeout recovery failed, injecting placeholder and continuing",
                            name,
                        )
                        # Reset recovery counter so next timeout also gets a retry chance
                        _timeout_recovery_count = 0
                        continue  # enter auto-wait, agent stays alive

                    else:
                        # Repeated timeout (shouldn't normally reach here due to counter reset above)
                        err_short_disp = f"LLM 超时 ({_base_timeout}s)"
                        await tracker.set_state(name, "error", reason=err_short_disp[:40])
                        await engine._send(f"  ✗ {name} timeout ({latency:.1f}s): {err_short_disp}")

                else:  # is_error
                    err_short = content[:150] if content else "Unknown error"
                    await tracker.set_state(name, "error", reason=err_short[:40])
                    await engine._send(f"  ✗ {name} failed ({latency:.1f}s): {err_short}")

                    _consecutive_error_count += 1
                    if _consecutive_error_count >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            "Broadcast: {} hit {} consecutive LLM errors, forcing exit",
                            name, _consecutive_error_count,
                        )
                        await engine._send(
                            f"  ✗ {name} 连续 {_consecutive_error_count} 次 LLM 错误，强制退出"
                        )
                        # If the leader crashes, end the entire group chat
                        # so other agents don't hang until timeout.
                        if is_leader:
                            _reason = f"Leader {name} 连续 {_consecutive_error_count} 次 LLM 错误"
                            engine._leader_end_reason = _reason
                            engine._running = False
                            leader_end_event.set()
                            logger.warning(
                                "Broadcast: leader %s force-exited, ending group chat: %s",
                                name, _reason,
                            )
                        break

                    # Keep agent alive instead of killing it (mirrors timeout recovery)
                    _placeholder = (
                        f"⚠️ [{name}] LLM调用出错（{err_short[:60]}），我仍在线。"
                        f"等待队友消息后将继续工作。"
                    )
                    content = _placeholder
                    commit_agent_turn(engine, name, _placeholder)
                    deliver(bus, name, ["All"], _placeholder)
                    await engine._send(
                        _d.chatroom_send_msg(
                            name, "错误恢复", _placeholder, max_len=1000, leader=leader_name
                        )
                    )
                    await tracker.set_state(name, "waiting", detail="error recovery")
                    logger.warning(
                        "Broadcast: {} LLM error ({}/{}), injecting placeholder and continuing",
                        name, _consecutive_error_count, MAX_CONSECUTIVE_ERRORS,
                    )
                    continue  # stay alive like timeout branch


            _used_chatroom_send = result.tools_used and "chatroom_send" in result.tools_used

            # Reset consecutive error counter on successful cycle
            _consecutive_error_count = 0

            # Record final text or tool calls in history
            if content or result.tool_calls_detail:
                if content:
                    logger.info(
                        "broadcast [{}] cycle {} output ({}c): {}",
                        name, cycle, len(content), content,
                    )
                history_content = commit_agent_turn(
                    engine, name, content, result.tool_calls_detail
                )

                # Track output for search pool credit recovery
                if content:
                    search_pool.on_output(name)

                if not _used_chatroom_send and content:
                    # Implicitly broadcast text to wake up waiting teammates (like the Leader).
                    # Without this, if an agent forgets to use chatroom_send, its text is only
                    # added to history and teammates hang in wait() until a full timeout.

                    _implicit_targets = ["All"]
                    if leader_name and name != leader_name:
                        # 队友未用 chatroom_send 时，默认只汇报给 Leader，避免唤醒其他正在 wait 的队友导致死循环
                        _implicit_targets = [leader_name]

                    deliver(bus, name, _implicit_targets, content)
                    await _trigger_realtime_interrupts(
                        sender=name,
                        targets=_implicit_targets,
                        mailbox=mailbox,
                        engine=engine,
                        leader_name=leader_name,
                    )

            # ── Shadow: post_error_guard decision ──
            _shadow_guard_ctx = CycleContext(
                agent_name=name,
                is_leader=is_leader,
                cycle=cycle,
                max_cycles=max_cycles,
                total_agents=total,
                engine_running=engine._running,
                discussion_ended=(mailbox.is_discussion_ended() if mailbox else False),
                leader_ended_discussion=_leader_ended_discussion,
                leader_end_event_set=leader_end_event.is_set() if leader_end_event else False,
                finish_reason=result.finish_reason,
                content=content,
                tools_used=tuple(result.tools_used or []),
                substantive_tools=_substantive_tools,
                timeout_recovery_count=_timeout_recovery_count,
                consecutive_error_count=_consecutive_error_count,
                max_consecutive_errors=MAX_CONSECUTIVE_ERRORS,
            )
            _shadow_guard = _cycle_ctrl.decide_post_error_guard(_shadow_guard_ctx)
            # Check oracle vs existing logic (if/elif chain)
            _guard_is_interrupted = is_interrupted
            _guard_is_idle = cycle == 1 and not content and not (set(result.tools_used or []) & _substantive_tools)
            _guard_no_text_after_tools = not content and (set(result.tools_used or []) & _substantive_tools) and "chatroom_send" not in (result.tools_used or [])
            _guard_leader_mgmt_only = is_leader and not content and result.tools_used and "chatroom_send" not in (result.tools_used or []) and not (set(result.tools_used or []) & _substantive_tools)
            _guard_ok = (
                (_guard_is_interrupted and _shadow_guard.action is CycleAction.INTERRUPT_CONTINUE) or
                (_guard_is_idle and _shadow_guard.action is CycleAction.IDLE_WARNING_CONTINUE) or
                (_guard_no_text_after_tools and _shadow_guard.action is CycleAction.NO_TEXT_AFTER_TOOLS_CONTINUE) or
                (_guard_leader_mgmt_only and _shadow_guard.action is CycleAction.LEADER_MGMT_NO_TEXT_CONTINUE) or
                (not _guard_is_interrupted and not _guard_is_idle and not _guard_no_text_after_tools and not _guard_leader_mgmt_only and _shadow_guard.action is CycleAction.PROCEED_TO_DISPLAY)
            )
            if not _guard_ok:
                logger.error(
                    "SHADOW MISMATCH @ post_error_guard: finish={} cycle={} content_len={} tools={} oracle={} is_int={} is_idle={} no_text={} leader_mgmt={}",
                    result.finish_reason, cycle, len(content), result.tools_used, _shadow_guard.action,
                    _guard_is_interrupted, _guard_is_idle, _guard_no_text_after_tools, _guard_leader_mgmt_only,
                )
            # ── End shadow: post_error_guard ──

            # ── Handle forced interrupt ──
            if is_interrupted:
                # Clear the interrupt event + reset the per-round counter so
                # newer messages can re-interrupt in subsequent cycles
                # (freshness guarantee). Owned by the runner now.
                _runner.acknowledge_interrupt()
                # (agent is already idle — the try/finally around tool_loop handled it)

                # Drain the entire queue and use the LATEST message so the
                # agent always responds to the most recent state, not a
                # stale message that happened to arrive first (FIFO).
                _intr_q = mailbox._queues.get(name)
                _intr_all: list = []
                if _intr_q:
                    while not _intr_q.empty():
                        try:
                            _intr_all.append(_intr_q.get_nowait())
                        except Exception:
                            break
                # Latest message is the one we respond to; earlier ones
                # become background context.
                _intr_msg = _intr_all[-1] if _intr_all else None
                _intr_earlier = _intr_all[:-1] if len(_intr_all) > 1 else []

                # UI: show who interrupted whom, with distinct label for user vs agent
                # Fallback to mailbox._last_interrupt_sender because the actual message
                # may have been consumed by wait() before drain runs.
                _sender_name = (_intr_msg.sender if _intr_msg
                                else mailbox._last_interrupt_sender.get(name, "teammate"))
                # Defensive: skip displaying self-interrupt (redundant/noop)
                if _sender_name == name:
                    _sender_name = "teammate"
                # Attach rank badge for debugging interrupt hierarchy violations
                def _badge(a: str) -> str:
                    r = ranks_map.get(a, "?")
                    return f"{a}[{r}]"
                await tracker.set_state(name, "interrupted", detail=f"from {_sender_name}")
                if _sender_name == "用户":
                    await engine._send(
                        f"⚡ {_badge(name)} 被【用户消息】打断，正在立即响应..."
                    )
                elif is_leader and _sender_name != "用户":
                    await engine._send(
                        f"⚡ {_badge(name)}（Leader）被队友 **{_badge(_sender_name)}** 汇报实时打断，正在响应..."
                    )
                else:
                    await engine._send(
                        f"⚡ {_badge(name)} 被 {_badge(_sender_name)} 的消息打断，正在立即响应..."
                    )
                logger.info(
                    "Broadcast: ⚡ {} interrupted by {} mid-turn (cycle {})",
                    name, _sender_name, cycle,
                )

                # Save any partial content already produced this cycle
                if content:
                    commit_agent_turn(
                        engine, name, content, result.tool_calls_detail
                    )
                    search_pool.on_output(name)
                    # Don't re-display partial content — it may be incomplete/mid-thought

                # ── Refresh working memory from shared History ──
                # Partial output (if any) is already committed. Rebuild from
                # History so the next tool_loop sees teammates/user commits
                # made during this turn, not a drifted private message list.
                trailing: list[dict[str, Any]] = []
                if _intr_earlier:
                    _earlier_lines = "\n".join(
                        f"- [{m.sender}]: {m.content[:200]}" for m in _intr_earlier
                    )
                    trailing.append({
                        "role": "system",
                        "content": (
                            f"[打断期间积压的 {len(_intr_earlier)} 条较早消息（仅供参考）]\n"
                            f"{_earlier_lines}\n"
                            f"请重点关注下面的最新消息。"
                        ),
                    })
                if _intr_msg:
                    trailing.append({
                        "role": "user",
                        "content": f"[{_intr_msg.sender} — 最新消息]: {_intr_msg.content}",
                    })
                else:
                    # Fallback: no message in queue (already consumed by auto-wait?)
                    trailing.append({
                        "role": "system",
                        "content": (
                            "[打断通知] 你的执行被中断，请立即总结当前进展并响应队友的最新需求。"
                        ),
                    })
                messages = wm.refresh(_build_prompt_snapshot, trailing=trailing)
                _sys_msg_count = wm.sys_msg_count
                volatile_msg_idx = wm.volatile_index

                await tracker.set_state(name, "thinking")
                _runner.begin_cycle()
                content = ""  # reset for the new cycle
                continue  # re-enter tool_loop with History-refreshed context

            # ── Anti-idle guard: force re-entry if agent did nothing ──
            if cycle == 1 and not content and not (set(result.tools_used or []) & _substantive_tools):
                logger.warning(
                    "Broadcast: {} idle on cycle 1 (no content, tools={}), forcing retry",
                    name, result.tools_used,
                )
                messages = wm.refresh(
                    _build_prompt_snapshot,
                    trailing=[{
                        "role": "system",
                        "content": get_system_warning("idle", name=name),
                    }],
                )
                _sys_msg_count = wm.sys_msg_count
                volatile_msg_idx = wm.volatile_index
                continue  # skip auto-wait, re-enter tool_loop

            # ── Guard: used tools but produced no text ──
            # Agent ran substantive tools but finished without writing any text.
            # Force a summary cycle so the output is not silently swallowed.
            elif not content and (set(result.tools_used or []) & _substantive_tools) and "chatroom_send" not in (result.tools_used or []):
                logger.warning(
                    "Broadcast: {} used tools on cycle {} but produced no text (tools={}), forcing summary",
                    name, cycle, result.tools_used,
                )
                messages = wm.refresh(
                    _build_prompt_snapshot,
                    trailing=[{
                        "role": "system",
                        "content": get_system_warning("no_text_after_tools", name=name),
                    }],
                )
                _sys_msg_count = wm.sys_msg_count
                volatile_msg_idx = wm.volatile_index
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
                messages = wm.refresh(
                    _build_prompt_snapshot,
                    trailing=[{
                        "role": "system",
                        "content": get_system_warning("leader_no_text_after_tools", name=name),
                    }],
                )
                _sys_msg_count = wm.sys_msg_count
                volatile_msg_idx = wm.volatile_index
                continue  # re-enter tool_loop to produce synthesis text

            # ── Auto-wait: enter idle state ──
            # Display the agent's final text for this cycle.
            # If chatroom_send was used, the content was already shown at tool-call
            # time (line 612) — skip the duplicate "Output" display.
            # Defer display when leader is in synthesis validation — only show
            # output that passes the quality gate (prevents spamming failed retries).
            if content and not (is_leader and _leader_ended_discussion):
                if not _used_chatroom_send:
                    # Token + latency suffix
                    tok = result.token_usage
                    total_tok = tok.get("total", 0)
                    tok_suffix = ""
                    if total_tok > 0:
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

                    # Finalize the streaming message: edit the same message
                    # in place (drop the ▍ cursor) instead of sending a new
                    # one — eliminates the duplicate-message display and
                    # keeps the full content visible (no 4000-char truncation
                    # of later deltas; finalize caps at 4096 once, cleanly).
                    await _stream.finalize(content + tok_suffix, fallback_send=engine._send)
                    logger.info("Broadcast: displayed {} cycle {} output ({} chars) [Local Only]", name, cycle, len(content))
                # else: chatroom_send already displayed the message — no duplicate needed

            # If leader called end_discussion this cycle, validate synthesis length & quality.
            # When finish_reason is "end_discussion", tool_loop broke before the LLM
            # could generate post-tool text — skip synthesis validation and exit cleanly.
            # EXCEPTION: if leader disabled/kicked an agent in the same cycle, the
            # clean-exit path was likely used to bypass the waiting guard.  Force a
            # synthesis retry so the user still gets a proper summary.

            # ── Shadow: leader_or_single_exit decision ──
            _shadow_exit_ctx = CycleContext(
                agent_name=name,
                is_leader=is_leader,
                cycle=cycle,
                max_cycles=max_cycles,
                total_agents=total,
                engine_running=engine._running,
                discussion_ended=(mailbox.is_discussion_ended() if mailbox else False),
                leader_ended_discussion=_leader_ended_discussion,
                leader_end_event_set=leader_end_event.is_set() if leader_end_event else False,
                finish_reason=result.finish_reason,
                content=content,
                tools_used=tuple(result.tools_used or []),
                substantive_tools=_substantive_tools,
                timeout_recovery_count=_timeout_recovery_count,
                consecutive_error_count=_consecutive_error_count,
                max_consecutive_errors=MAX_CONSECUTIVE_ERRORS,
            )
            _shadow_exit = _cycle_ctrl.decide_leader_or_single_exit(_shadow_exit_ctx)
            # Check oracle vs existing logic
            _exit_is_leader_ended = is_leader and _leader_ended_discussion
            _exit_leader_no_text = _exit_is_leader_ended and not content
            _exit_leader_has_text = _exit_is_leader_ended and bool(content)
            _exit_single_agent = total == 1
            _exit_ok = (
                (_exit_leader_no_text and _shadow_exit.action is CycleAction.LEADER_END_NO_TEXT_CONTINUE) or
                (_exit_leader_has_text and _shadow_exit.action is CycleAction.LEADER_END_DISPLAY_BREAK) or
                (not _exit_is_leader_ended and _exit_single_agent and _shadow_exit.action is CycleAction.SINGLE_AGENT_BREAK) or
                (not _exit_is_leader_ended and not _exit_single_agent and _shadow_exit.action is CycleAction.PROCEED_TO_AUTO_WAIT)
            )
            if not _exit_ok:
                logger.error(
                    "SHADOW MISMATCH @ leader_or_single_exit: is_leader={} leader_ended={} content_len={} total={} oracle={}",
                    is_leader, _leader_ended_discussion, len(content), total, _shadow_exit.action,
                )
            # ── End shadow: leader_or_single_exit ──

            if is_leader and _leader_ended_discussion:
                # Stable behavior: if end_discussion produced no text, force ONE
                # synthesis cycle; otherwise display whatever was produced and exit.
                # The previous length-gated + quality-gated retry loop (up to 3 full
                # tool_loop calls) caused severe end-of-discussion stalls; the
                # max_cycles cap is the only backstop needed.
                if not content:
                    logger.warning(
                        "Broadcast: leader {} called end_discussion without text (cycle {}), forcing synthesis",
                        name, cycle,
                    )
                    messages = wm.refresh(
                        _build_prompt_snapshot,
                        trailing=[{
                            "role": "system",
                            "content": get_system_warning("leader_end_without_text", name=name),
                        }],
                    )
                    _sys_msg_count = wm.sys_msg_count
                    volatile_msg_idx = wm.volatile_index
                    continue  # re-enter tool_loop to produce synthesis text
                # Synthesis produced — display it (always, even if chatroom_send
                # was used: chatroom_send targets teammates, this is the user's
                # only delivery channel and must never be silently dropped).
                tok = result.token_usage
                total_tok = tok.get("total", 0)
                tok_suffix = ""
                if total_tok > 0:
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
                # Finalize streaming message in place (no duplicate send).
                await _stream.finalize(content + tok_suffix, fallback_send=engine._send)
                logger.info("Broadcast: displayed {} synthesis output ({} chars)", name, len(content))
                logger.info("Broadcast: leader {} called end_discussion, exiting cycle loop", name)
                break

            # Single agent: no teammates to wait for, exit immediately
            if total == 1:
                logger.info("Broadcast: {} single agent mode, exiting cycle loop", name)
                break

            # Now wait for teammate messages
            await tracker.set_state(name, "waiting")
            logger.info("Broadcast: {} entering auto-wait (cycle {})", name, cycle)
            # Release unread pool slots before waiting (mirrors WaitTool behavior)
            # Without this, slots consumed by messages sent TO this agent are never
            # freed, causing pool exhaustion and blocking other agents' replies.
            if pool:
                pool.release_unread(name)

            # State management: mark as waiting (moved from mailbox.wait)
            _runner.set_waiting(True)

            # Check for all-waiting deadlock (moved from mailbox)
            _active = [n for n, r in engine._runners.items() if r.state != "done"]
            _all_waiting = all(r.is_waiting for r in engine._runners.values() if r.state != "done")
            if _all_waiting and _active:
                logger.warning("Broadcast: all {} agents waiting — deadlock detected", len(_active))
                # Nudge a random agent to break deadlock
                _target = random.choice(_active)
                _nudge_evt = engine._runners[_target].interrupt_event
                if not _nudge_evt.is_set():
                    _nudge_evt.set()
                    logger.info("Broadcast: nudging {} to break deadlock", _target)

            msg = await mailbox.wait(name, timeout=600)
            _runner.set_waiting(False)

            # ── Shadow: after_wait decision ──
            _shadow_wait_ctx = CycleContext(
                agent_name=name,
                is_leader=is_leader,
                cycle=cycle,
                max_cycles=max_cycles,
                total_agents=total,
                engine_running=engine._running,
                discussion_ended=(mailbox.is_discussion_ended() if mailbox else False),
                leader_ended_discussion=_leader_ended_discussion,
                leader_end_event_set=leader_end_event.is_set() if leader_end_event else False,
                finish_reason=result.finish_reason,
                content=content,
                tools_used=tuple(result.tools_used or []),
                substantive_tools=_substantive_tools,
                timeout_recovery_count=_timeout_recovery_count,
                consecutive_error_count=_consecutive_error_count,
                max_consecutive_errors=MAX_CONSECUTIVE_ERRORS,
                wait_msg=msg,
            )
            _shadow_wait = _cycle_ctrl.decide_after_wait(_shadow_wait_ctx)
            # Check oracle vs existing logic
            _wait_none = msg is None
            _wait_none_ended = _wait_none and (not engine._running or (leader_end_event and leader_end_event.is_set()) or (mailbox and mailbox.is_discussion_ended()))
            _wait_none_leader_no_text = _wait_none and not _wait_none_ended and is_leader and not content
            _wait_none_nonleader = _wait_none and not _wait_none_ended and not _wait_none_leader_no_text
            _wait_msg_stopped = msg is not None and (not engine._running or (leader_end_event and leader_end_event.is_set()))
            _wait_msg_inject = msg is not None and not _wait_msg_stopped
            _wait_ok = (
                (_wait_none_ended and _shadow_wait.action is CycleAction.WAIT_NONE_ENDED_BREAK) or
                (_wait_none_leader_no_text and _shadow_wait.action is CycleAction.WAIT_NONE_LEADER_SYNTHESIS_CONTINUE) or
                (_wait_none_nonleader and _shadow_wait.action is CycleAction.WAIT_NONE_NONLEADER_CONTINUE) or
                (_wait_msg_stopped and _shadow_wait.action is CycleAction.WAIT_MSG_STOPPED_BREAK) or
                (_wait_msg_inject and _shadow_wait.action is CycleAction.WAIT_MSG_INJECT_CONTINUE)
            )
            if not _wait_ok:
                logger.error(
                    "SHADOW MISMATCH @ after_wait: msg_is_none={} running={} leader_end={} disc_ended={} is_leader={} content_len={} oracle={}",
                    msg is None, engine._running,
                    leader_end_event.is_set() if leader_end_event else False,
                    mailbox.is_discussion_ended() if mailbox else False,
                    is_leader, len(content), _shadow_wait.action,
                )
            # ── End shadow: after_wait ──

            if msg is None:
                # No message — check if engine stopped or leader ended discussion
                ended = (not engine._running or leader_end_event.is_set() or
                         (mailbox and getattr(mailbox, "is_discussion_ended", lambda: False)()))
                if ended:
                    await tracker.set_state(name, "done", reason="discussion ended")
                    logger.info("Broadcast: {} wait returned None, discussion ended, exiting", name)
                    break
                # Leader fallback: if no text was produced, force synthesis
                if is_leader and not content:
                    logger.warning(
                        "Broadcast: leader {} wait timeout with no text (cycle {}), forcing synthesis",
                        name, cycle,
                    )
                    messages = wm.refresh(
                        _build_prompt_snapshot,
                        trailing=[{
                            "role": "system",
                            "content": get_system_warning("leader_wait_timeout", name=name),
                        }],
                    )
                    _sys_msg_count = wm.sys_msg_count
                    volatile_msg_idx = wm.volatile_index
                    continue  # re-enter tool_loop for synthesis
                # Non-leader: keep waiting (stable behavior). The mailbox's
                # all-waiting nudge + leader end_discussion are the only exit
                # paths. A force-exit here causes cascading stalls when other
                # agents are still expecting this agent to reply.
                logger.info("Broadcast: {} wait timeout, retrying wait", name)
                continue

            # Got a message! Inject it and re-run tool_loop
            # But first check if /stop was issued or leader ended discussion
            if not engine._running or leader_end_event.is_set():
                logger.info("Broadcast: {} exiting after wait — engine stopped", name)
                break
            logger.info("Broadcast: {} reactivated by {}: {}", name, msg.sender, msg.content[:60])
            await tracker.set_state(name, "thinking")
            await engine._send(_d.chatroom_wait_msg(name, str(msg), leader=leader_name))

            # ── Refresh working memory from shared History ──
            # Prior cycle already committed via commit_agent_turn; teammates'
            # commits during our wait are also in History. Rebuild the LLM
            # session from History instead of prune+append on a private list.
            trailing: list[dict[str, Any]] = []
            _anti_repeat_tag = f"[提醒] 你（{name}）已经发表过上述观点"
            trailing.append({
                "role": "system",
                "content": (
                    f"{_anti_repeat_tag}。"
                    f"针对队友的新消息做出回应或补充新观点，不要重复已说的内容。"
                ),
            })
            trailing.append({
                "role": "user",
                "content": f"[队友消息] {msg}",
            })
            messages = wm.refresh(_build_prompt_snapshot, trailing=trailing)
            _sys_msg_count = wm.sys_msg_count
            volatile_msg_idx = wm.volatile_index

        # ── Final completion ──
        await tracker.set_state(name, "done")
        comp = _d.completion_msg(name, round(total_latency, 1), total_iterations, all_tools_used, leader=leader_name)
        if comp:
            await engine._send(comp)

        log_request(engine, name, model, "broadcast",
                    reply_len=len(content) if content else 0,
                    tools=all_tools_used, iterations=total_iterations,
                    latency=round(total_latency, 1))
        return (name, content, all_tools_used, {})

    except asyncio.CancelledError:
        # Cancelled by leader end_discussion or engine stop
        await tracker.set_state(name, "cancelled")
        comp = _d.completion_msg(name, round(total_latency, 1), total_iterations, all_tools_used, leader=leader_name)
        if comp:
            await engine._send(comp)
        return (name, content, all_tools_used, {})

    except Exception as e:
        await tracker.set_state(name, "error", reason=str(e)[:40])
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

