"""Single-agent execution loop for broadcast rounds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.groupchat.orchestra.broadcast_context import BroadcastContext
from nanobot.groupchat.orchestra.broadcast_status import AgentStatusTracker
from nanobot.groupchat.orchestra.chat_utils import build_tool_log, log_request
from nanobot.groupchat.orchestra.events import trigger_realtime_interrupts
from nanobot.groupchat.orchestra.mailbox import MailboxHub
from nanobot.groupchat.history.component_manager import (
    get_system_warning,
    synthesis_quality_check,
    _MIN_SYNTHESIS_LEN,
)
from nanobot.groupchat.history.message_converter import latest_user_question


def _resolve_loop_limits(*, is_leader: bool, total: int) -> tuple[int, int]:
    """Return (max_tool_iterations, max_cycles) for one broadcast agent."""
    agent_max_iters = 12 if is_leader else 8
    max_cycles = 30 if is_leader else 20
    if total <= 1:
        return min(agent_max_iters, 4), min(max_cycles, 3)
    return agent_max_iters, max_cycles


def _resolve_call_timeout(gc_settings: dict, *, is_leader: bool, total: int) -> float | None:
    """Resolve LLM call timeout, capped for single-agent broadcast."""
    key = "leader_call_timeout" if is_leader else "call_timeout"
    timeout = float(gc_settings.get(key, 90)) or 0.0
    if timeout <= 0:
        return None
    if total <= 1:
        timeout = min(timeout, 75.0)
    return timeout


def _rebuild_prompt_prefix(
    engine: Any,
    name: str,
    *,
    agent_ranks: dict[str, int],
    agent_idx: int,
    total: int,
    teammates: list[str],
    user_question: str,
    is_leader: bool,
    leader_name: str | None,
    non_leader_agents: list[str],
) -> tuple[list[dict], str]:
    """Rebuild stable prompt prefix from live ``engine._history`` (tier-trimmed)."""
    live_uq = latest_user_question(engine._history) or user_question
    fresh = engine._build_agent_prompt(
        name,
        relevant_agents=None,
        agent_ranks=agent_ranks,
        agent_idx=agent_idx,
        total=total,
        teammates=teammates,
        user_question=live_uq,
    )
    _apply_broadcast_runtime_inserts(
        fresh,
        is_leader=is_leader,
        leader_name=leader_name,
        non_leader_agents=non_leader_agents,
        engine=engine,
    )
    return fresh, live_uq


def _apply_broadcast_runtime_inserts(
    messages: list[dict],
    *,
    is_leader: bool,
    leader_name: str | None,
    non_leader_agents: list[str],
    engine: Any,
) -> None:
    """Leader / non-leader runtime system inserts before the volatile tail."""
    if is_leader:
        agent_caps = []
        for a in non_leader_agents:
            on = engine.get_agent_enabled_tool_names(a)
            agent_caps.append(f"  {a}: {', '.join(on) if on else '(无工具)'}")

        leader_on = engine.get_agent_enabled_tool_names(leader_name)
        leader_base_tools_str = f"（{', '.join(leader_on)}）" if leader_on else "（无基础工具）"

        messages.insert(max(len(messages) - 1, 0), {
            "role": "system",
            "content": (
                f"[Leader 本轮上下文]\n"
                f"## 团队成员及工具能力\n"
                + "\n".join(agent_caps) + "\n"
                f"你的基础工具{leader_base_tools_str}\n"
                f"⚠️ 只分配队友有工具能力完成的任务。权限与搜索额度见末尾 [本轮状态汇总]。"
            ),
        })
    elif leader_name:
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

    perm_insert_idx = max(len(messages) - 1, 0)
    messages.insert(perm_insert_idx, {
        "role": "system",
        "content": "[团队工具权限及搜索额度见消息末尾 [本轮状态汇总]]",
    })


@dataclass
class AgentTurnContext:
    engine: BroadcastContext
    agents: list[str]
    leader_name: str | None
    non_leader_agents: list[str]
    total: int
    agent_ranks: dict[str, int]
    user_question: str
    mailbox: MailboxHub
    pool: Any
    tracker: AgentStatusTracker
    search_pool: Any
    agent_tool_registries: dict[str, Any]
    gc_settings: dict[str, Any]
    view: Any
    leader_end_event: asyncio.Event
    exec_agents: list[str]
    ranks_map: dict[str, str]


async def run_agent_turn(
    name: str,
    agent_idx: int,
    ctx: AgentTurnContext,
) -> tuple[str, str | None, list[str], dict]:
    """Run a single agent with streaming display."""
    engine = ctx.engine
    agents = ctx.agents
    leader_name = ctx.leader_name
    non_leader_agents = ctx.non_leader_agents
    total = ctx.total
    agent_ranks = ctx.agent_ranks
    user_question = ctx.user_question
    mailbox = ctx.mailbox
    pool = ctx.pool
    tracker = ctx.tracker
    search_pool = ctx.search_pool
    agent_tool_registries = ctx.agent_tool_registries
    gc_settings = ctx.gc_settings
    view = ctx.view
    leader_end_event = ctx.leader_end_event
    exec_agents = ctx.exec_agents
    ranks_map = ctx.ranks_map

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
    messages = engine._build_agent_prompt(
        name,
        relevant_agents=None,
        agent_ranks=agent_ranks,
        agent_idx=agent_idx,
        total=total,
        teammates=teammates,
        user_question=user_question,
    )

    is_leader = (name == leader_name)
    _leader_ended_discussion = False
    _leader_disabled_agent = False  # track if leader disabled/kicked an agent this cycle
    _synthesis_retries = 0  # guard against infinite synthesis retry loops
    # Load from override system (editable via /prompt), fallback to default
    # Removed stale prompt_overrides.json lookup; .md files are the source of truth.

    _apply_broadcast_runtime_inserts(
        messages,
        is_leader=is_leader,
        leader_name=leader_name,
        non_leader_agents=non_leader_agents,
        engine=engine,
    )

    # The volatile state message is always the last one (added by PromptBuilder)
    volatile_msg_idx = len(messages) - 1

    # ── Edit-in-place display (broadcast mode) ──
    # Each tool call gets one message (🟡), then edited with result (🟢/🔴).
    _tool_lines: list[str] = []
    _pending_tool_msgs: dict[str, tuple[int | None, str]] = {}  # tool_call_id → (msg_id, original_text)
    # Shared state between _on_tool_start and _on_tool_result for chatroom_send args
    _last_chatroom_send_to: list[str] = []

    badge = f" [{agent_idx + 1}/{total}]"
    _header = f"◍ {name}{badge}: "

    # Send initial status
    await engine._send(_d.thinking_msg(name, model_short, leader=leader_name, idx=agent_idx + 1, total=total), progress=True)
    async def _on_tool_start(tool_name: str, args: dict, **_kw) -> None:
        tool_call_id = _kw.get("tool_call_id", "")
        if not isinstance(args, dict):
            args = {}
        await view.on_tool_start(name, tool_name, args, tool_call_id, _cycle_t0, _cycle_usage)

    async def _on_tool_result(tool_name: str, tool_call_id: str, result: str) -> None:
        await view.on_tool_result(name, tool_name, tool_call_id, result)


    # No streaming callbacks — broadcast uses non-streaming mode
    # ── Run tool-loop + auto-wait cycle ──
    # After tool_loop finishes, agent automatically enters wait().
    # If a teammate message arrives, inject it and re-run tool_loop.
    # Only exits when cancelled by leader end_discussion, /stop, or on error.
    from nanobot.groupchat.orchestra.tools.tool_loop import tool_loop

    # Load configurable result_max_chars for broadcast mode
    try:
        from nanobot.groupchat.history.history_settings import broadcast_result_max_chars
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
    agent_max_iters, max_cycles = _resolve_loop_limits(
        is_leader=is_leader, total=total,
    )
    _substantive_tools = {"web_search", "web_fetch", "exec", "read_file", "write_file"}
    # Separate system-prompt messages (stable prefix) from conversation messages
    # so we can prune conversation turns without touching the system prompt.
    _sys_msg_count = len(messages)
    _prefix_history_len = len(engine._history)

    def _apply_live_prefix(cycle_tail: list[dict], *, reason: str) -> None:
        nonlocal messages, _sys_msg_count, volatile_msg_idx, user_question, _prefix_history_len
        fresh_prefix, live_uq = _rebuild_prompt_prefix(
            engine,
            name,
            agent_ranks=agent_ranks,
            agent_idx=agent_idx,
            total=total,
            teammates=teammates,
            user_question=user_question,
            is_leader=is_leader,
            leader_name=leader_name,
            non_leader_agents=non_leader_agents,
        )
        messages[:] = fresh_prefix + cycle_tail
        _sys_msg_count = len(fresh_prefix)
        volatile_msg_idx = len(fresh_prefix) - 1
        user_question = live_uq
        _prefix_history_len = len(engine._history)
        logger.info(
            "Broadcast: {} rebuilt prompt prefix ({}, history={} msgs, uq={!r})",
            name, reason, len(engine._history), live_uq[:60],
        )

    # ── Consecutive wait-timeout tracker ──
    # Prevents agents from looping wait→timeout→wait forever when no one
    # is going to reply.  After MAX_CONSECUTIVE_WAITS empty waits, exit.
    _consecutive_waits = 0
    MAX_CONSECUTIVE_WAITS = 3

    # ── Forced interrupt: get this agent's interrupt event from mailbox ──
    _interrupt_event = mailbox.get_interrupt_event(name)
    # Tracks how many timeout-recovery attempts this agent has made.
    # Hard cap at 1 to prevent recovery loops.
    _timeout_recovery_count = 0
    # Tracks consecutive LLM errors to prevent rapid-fire error loops.
    # After MAX_CONSECUTIVE_ERRORS, the agent exits instead of continuing.
    _consecutive_error_count = 0
    MAX_CONSECUTIVE_ERRORS = 3

    # ── Synthesis retry helper ────────────────────────────────────────
    async def _inject_retry(prompt: str) -> bool:
        """Inject retry prompt; return True if caller should continue, False if exhausted and should break."""
        messages.append({"role": "system", "content": prompt})
        nonlocal _synthesis_retries
        _synthesis_retries += 1
        if _synthesis_retries >= 3:
            logger.warning("Broadcast: leader {} synthesis retry exhausted ({} attempts), forcing exit", name, _synthesis_retries)
            return False
        engine._running = True
        return True

    try:
        while True:
            # Hard cycle cap — prevent runaway agents from draining resources
            if cycle >= max_cycles:
                logger.warning(
                    "Broadcast: {} hit max_cycles={}, forcing exit", name, max_cycles
                )
                if not content:
                    messages.append({
                        "role": "system",
                        "content": "[已达到最大轮次限制，请立即输出最终总结，禁止再调用工具。]",
                    })
                    try:
                        _r = await tool_loop(
                            provider=engine.provider,
                            messages=messages,
                            tool_registry=reg,
                            model=model,
                            max_tokens=engine.config.max_tokens,
                            max_iterations=1,
                            tool_defs=list(broadcast_defs) if broadcast_defs else None,
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
            _cycle_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

            async def _on_iter_usage(usage: dict) -> None:
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    _cycle_usage[k] += usage.get(k, 0)

            # ── Pre-tool_loop pruning: cover all cycle paths (not just wait) ──
            _conv_keep_turns = gc_settings.get("conv_keep_turns", 3)
            _max_conv_msgs = _sys_msg_count + (_conv_keep_turns * 3) + 6
            if len(messages) > _max_conv_msgs:
                from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary
                from nanobot.groupchat.history.history_settings import summarize_model as _summarize_model
                dropped = await prune_conversation_tail_with_summary(
                    messages, _sys_msg_count, _conv_keep_turns,
                    provider=engine.provider,
                    model=_summarize_model(),
                    agent_name=name,
                )
                if dropped > 0:
                    logger.debug(
                        "Broadcast: {} pre-tool_loop pruned {} msgs (len {} → {})",
                        name, dropped, len(messages) + dropped, len(messages),
                    )

            # Mark agent busy so incoming messages can trigger interrupt
            mailbox.mark_busy(name)
            try:
                result = await tool_loop(
                    provider=engine.provider,
                    messages=messages,
                    tool_registry=reg,
                    model=model,
                    max_tokens=engine.config.max_tokens,
                    max_iterations=agent_max_iters,
                    tool_defs=tool_defs if tool_defs else None,
                    reasoning_effort=_live_cfg.get("reasoning_effort") or None,
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
                    result_max_chars=_broadcast_result_max,
                    call_timeout=_resolve_call_timeout(
                        gc_settings, is_leader=is_leader, total=total,
                    ),
                    interrupt_event=_interrupt_event,
                )
            finally:
                # Always mark idle when tool_loop exits (interrupt, stop, normal, error)
                mailbox.mark_idle(name)


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

            # Track if leader disabled/kicked an agent this cycle — this affects
            # the synthesis path (see L1234 clean-exit guard).
            if is_leader and "manage_agent" in (result.tools_used or []):
                for tc in (result.tool_calls_detail or []):
                    if tc.get("name") == "manage_agent":
                        _action = (tc.get("arguments") or {}).get("action", "")
                        if _action in ("disable", "restart"):
                            _leader_disabled_agent = True
                            break

            if is_error or is_timeout:
                if is_timeout:
                    _base_timeout = _resolve_call_timeout(
                        gc_settings, is_leader=is_leader, total=total,
                    ) or 90.0
                    err_short = f"LLM 超时 ({_base_timeout}s)"

                    # ── Clean retry on first timeout ──
                    # Keep tools enabled so write_file/exec tasks can finish;
                    # use a longer timeout on retry instead of stripping tools.
                    if _timeout_recovery_count == 0:
                        _timeout_recovery_count += 1
                        _retry_timeout = min(float(_base_timeout) * 2, 300.0)
                        await tracker.set_state(name, "thinking", detail="retry...")
                        await engine._send(f"⏰ {name} 超时，延长到 {_retry_timeout:.0f}s 重试...", progress=True)
                        logger.warning(
                            "Broadcast: {} LLM timeout ({:.1f}s), retrying once (tools kept, {:.0f}s)",
                            name, latency, _retry_timeout,
                        )
                        try:
                            _r = await tool_loop(
                                provider=engine.provider,
                                messages=messages,          # unchanged — no injection
                                tool_registry=reg,
                                model=model,
                                max_tokens=engine.config.max_tokens,
                                max_iterations=agent_max_iters,
                                tool_defs=tool_defs if tool_defs else None,
                                call_timeout=_retry_timeout,
                            )
                            if _r.content:
                                content = _r.content
                                total_latency += _r.latency
                                engine._add_message(name, content)
                                search_pool.on_output(name)
                                mailbox.send(name, ["All"], content[:300])
                                await engine._send(
                                    _d.chatroom_send_msg(
                                        name, "重试输出", content, max_len=1000, leader=leader_name
                                    ), progress=True,
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
                        engine._add_message(name, _placeholder)
                        mailbox.send(name, ["All"], _placeholder)
                        await engine._send(
                            _d.chatroom_send_msg(
                                name, "超时占位", _placeholder, max_len=1000, leader=leader_name
                            ), progress=True,
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
                        await engine._send(f"  ✗ {name} timeout ({latency:.1f}s): {err_short_disp}", progress=True)

                else:  # is_error
                    err_short = content[:150] if content else "Unknown error"
                    await tracker.set_state(name, "error", reason=err_short[:40])
                    await engine._send(f"  ✗ {name} failed ({latency:.1f}s): {err_short}", progress=True)

                    _consecutive_error_count += 1
                    if _consecutive_error_count >= MAX_CONSECUTIVE_ERRORS:
                        logger.error(
                            "Broadcast: {} hit {} consecutive LLM errors, forcing exit",
                            name, _consecutive_error_count,
                        )
                        await engine._send(
                            f"  ✗ {name} 连续 {_consecutive_error_count} 次 LLM 错误，强制退出", progress=True,
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
                    engine._add_message(name, _placeholder)
                    mailbox.send(name, ["All"], _placeholder)
                    await engine._send(
                        _d.chatroom_send_msg(
                            name, "错误恢复", _placeholder, max_len=1000, leader=leader_name
                        ), progress=True,
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
            # Reset consecutive wait counter — agent produced output
            _consecutive_waits = 0

            # Record final text or tool calls in history
            if content or result.tool_calls_detail:
                if content:
                    logger.info(
                        "broadcast [{}] cycle {} output ({}c): {}",
                        name, cycle, len(content), content,
                    )
                history_content = (content or "") + build_tool_log(result.tool_calls_detail)
                engine._add_message(name, history_content)

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
                        
                    mailbox.send(name, _implicit_targets, content)
                    await trigger_realtime_interrupts(
                        sender=name,
                        targets=_implicit_targets,
                        mailbox=mailbox,
                        engine=engine,
                        leader_name=leader_name,
                    )

            # ── Handle forced interrupt ──
            if is_interrupted:
                # Clear the event so it can be set again by a future message
                _interrupt_event.clear()
                # Reset interrupt counter so newer messages can re-interrupt
                # this agent in subsequent cycles (freshness guarantee).
                mailbox._interrupt_counts[name] = 0
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
                        f"⚡ {_badge(name)} 被【用户消息】打断，正在立即响应...", progress=True,
                    )
                elif is_leader and _sender_name != "用户":
                    await engine._send(
                        f"⚡ {_badge(name)}（Leader）被队友 **{_badge(_sender_name)}** 汇报实时打断，正在响应...", progress=True,
                    )
                else:
                    await engine._send(
                        f"⚡ {_badge(name)} 被 {_badge(_sender_name)} 的消息打断，正在立即响应...", progress=True,
                    )
                logger.info(
                    "Broadcast: ⚡ {} interrupted by {} mid-turn (cycle {})",
                    name, _sender_name, cycle,
                )

                # Save any partial content already produced this cycle
                if content:
                    history_content = content + build_tool_log(result.tool_calls_detail)
                    engine._add_message(name, history_content)
                    search_pool.on_output(name)
                    # Don't re-display partial content — it may be incomplete/mid-thought

                cycle_tail: list[dict[str, str]] = []
                if content:
                    cycle_tail.append({"role": "assistant", "content": content})

                if _intr_earlier:
                    _earlier_lines = "\n".join(
                        f"- [{m.sender}]: {m.content[:200]}" for m in _intr_earlier
                    )
                    cycle_tail.append({
                        "role": "system",
                        "content": (
                            f"[打断期间积压的 {len(_intr_earlier)} 条较早消息（仅供参考）]\n"
                            f"{_earlier_lines}\n"
                            f"请重点关注下面的最新消息。"
                        ),
                    })
                if _intr_msg:
                    cycle_tail.append({
                        "role": "user",
                        "content": f"[{_intr_msg.sender} — 最新消息]: {_intr_msg.content}",
                    })
                else:
                    cycle_tail.append({
                        "role": "system",
                        "content": "[打断通知] 你的执行被中断，请立即总结当前进展并响应队友的最新需求。",
                    })

                if _sender_name == "用户" or len(engine._history) > _prefix_history_len:
                    _apply_live_prefix(cycle_tail, reason=f"interrupt from {_sender_name}")
                else:
                    # ── Prune conversation tail (no history rebuild needed) ──
                    from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary
                    from nanobot.groupchat.history.history_settings import summarize_model as _summarize_model
                    _conv_keep_turns = gc_settings.get("conv_keep_turns", 3)
                    dropped = await prune_conversation_tail_with_summary(
                        messages, _sys_msg_count, _conv_keep_turns,
                        provider=engine.provider,
                        model=_summarize_model(),
                        agent_name=name,
                    )
                    if dropped > 0:
                        logger.debug(
                            "Broadcast: {} interrupt pruned {} msgs (kept {})",
                            name, dropped, _conv_keep_turns * 3,
                        )
                    messages.extend(cycle_tail)

                await tracker.set_state(name, "thinking")
                mailbox.mark_busy(name)
                content = ""  # reset for the new cycle
                continue  # re-enter tool_loop with injected message

            # ── Anti-idle guard: force re-entry if agent did nothing ──
            if cycle == 1 and not content and not (set(result.tools_used or []) & _substantive_tools):
                logger.warning(
                    "Broadcast: {} idle on cycle 1 (no content, tools={}), forcing retry",
                    name, result.tools_used,
                )
                messages.append({
                    "role": "system",
                    "content": get_system_warning("idle", name=name)
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
                    "content": get_system_warning("no_text_after_tools", name=name)
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
                    "content": get_system_warning("leader_no_text_after_tools", name=name)
                })
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

                    target_label = f"Output [{cycle}]"
                    await engine._send(_d.chatroom_send_msg(name, target_label, content + tok_suffix, max_len=3000, leader=leader_name), progress=True)
                    logger.info("Broadcast: displayed {} cycle {} output ({} chars) [Local Only]", name, cycle, len(content))
                # else: chatroom_send already displayed the message — no duplicate needed
            
            # If leader called end_discussion this cycle, validate synthesis length & quality.
            # When finish_reason is "end_discussion", tool_loop broke before the LLM
            # could generate post-tool text — skip synthesis validation and exit cleanly.
            # EXCEPTION: if leader disabled/kicked an agent in the same cycle, the
            # clean-exit path was likely used to bypass the waiting guard.  Force a
            # synthesis retry so the user still gets a proper summary.
            if is_leader and _leader_ended_discussion:
                if result.finish_reason == "end_discussion" and not content and not _leader_disabled_agent:
                    # Agent called end_discussion and tool_loop exited immediately.
                    # The agent's last substantive output was already displayed in a
                    # previous cycle — no synthesis retry needed.
                    logger.info(
                        "Broadcast: leader {} end_discussion with no post-tool content, exiting cleanly",
                        name,
                    )
                    break
                stripped = content.strip() if content else ""
                if len(stripped) < _MIN_SYNTHESIS_LEN:
                    logger.warning(
                    "Broadcast: leader {} synthesis too short ({} chars < {}), forcing retry",
                    name, len(stripped), _MIN_SYNTHESIS_LEN,
                    )
                    _tool_data = build_tool_log(result.tool_calls_detail)
                    _retry_prompt = get_system_warning("leader_end_without_text", name=name)
                    if _tool_data:
                        _retry_prompt += (
                            "\n\n[本轮工具调用结果 — 请基于以下数据输出总结]\n"
                            + _tool_data
                        )
                    if not await _inject_retry(_retry_prompt):
                        break
                    continue
                # Step 2 — content quality guard (catches fluff like "问题已解答，无需补充")
                quality_ok, quality_reason = synthesis_quality_check(stripped, tools_used=result.tools_used)
                if not quality_ok:
                    logger.warning(
                    "Broadcast: leader {} synthesis quality check failed ({})",
                    name, quality_reason,
                    )
                    # Pick the most specific warning template
                    if "记忆" in quality_reason or "memory" in quality_reason:
                        _warn = get_system_warning("delivery_gate_memory", name=name)
                    elif "数据采集" in quality_reason or "工具" in quality_reason:
                        _warn = get_system_warning("delivery_gate_tools", name=name)
                    else:
                        _warn = get_system_warning("leader_end_without_text", name=name)
                    _tool_data = build_tool_log(result.tool_calls_detail)
                    _retry_content = (
                        _warn
                        + f"\n\n[质量检查失败] {quality_reason}"
                        "\n请输出包含 ## 结论、## 关键发现 的结构化总结，附带具体数据和来源。"
                    )
                    if _tool_data:
                        _retry_content += (
                            "\n\n[本轮工具调用结果 — 请基于以下数据输出总结]\n"
                            + _tool_data
                        )
                    if not await _inject_retry(_retry_content):
                        break
                    continue
                # Synthesis passed validation — now display it
                # NOTE: Always display leader synthesis even if chatroom_send was used.
                # chatroom_send targets teammates, NOT the user. The final synthesis
                # is the user's only delivery channel and must never be silently dropped.
                if content:
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
                    target_label = f"Output [{cycle}]"
                    await engine._send(_d.chatroom_send_msg(name, target_label, content + tok_suffix, max_len=3000, leader=leader_name), progress=True)
                    logger.info("Broadcast: displayed {} synthesis output ({} chars) [post-validation]", name, len(content))
                logger.info("Broadcast: leader {} called end_discussion, exiting cycle loop", name)
                break

            # Now wait for teammate messages
            await tracker.set_state(name, "waiting")
            logger.info("Broadcast: {} entering auto-wait (cycle {})", name, cycle)
            # Release unread pool slots before waiting (mirrors WaitTool behavior)
            # Without this, slots consumed by messages sent TO this agent are never
            # freed, causing pool exhaustion and blocking other agents' replies.
            if pool:
                pool.release_unread(name)
            msg = await mailbox.wait(name, timeout=600)

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
                    messages.append({
                        "role": "system",
                        "content": get_system_warning("leader_wait_timeout", name=name)
                    })
                    _consecutive_waits = 0
                    continue  # re-enter tool_loop for synthesis
                # Consecutive wait-timeout guard: if this agent keeps timing
                # out with no incoming messages, it's stuck in a wait loop.
                # After MAX_CONSECUTIVE_WAITS, exit to avoid blocking the round.
                _consecutive_waits += 1
                if _consecutive_waits >= MAX_CONSECUTIVE_WAITS:
                    logger.warning(
                        "Broadcast: {} hit {} consecutive wait timeouts, exiting (no one replying)",
                        name, _consecutive_waits,
                    )
                    await tracker.set_state(name, "done", reason=f"wait timeout x{_consecutive_waits}")
                    break
                logger.info("Broadcast: {} wait timeout ({}/{}), retrying wait", name, _consecutive_waits, MAX_CONSECUTIVE_WAITS)
                continue

            # Got a message! Inject it and re-run tool_loop
            # But first check if /stop was issued or leader ended discussion
            if not engine._running or leader_end_event.is_set():
                logger.info("Broadcast: {} exiting after wait — engine stopped", name)
                break
            _consecutive_waits = 0  # reset — we got a real message
            logger.info("Broadcast: {} reactivated by {}: {}", name, msg.sender, msg.content[:60])
            await tracker.set_state(name, "thinking")
            await engine._send(_d.chatroom_wait_msg(name, str(msg), leader=leader_name), progress=True)

            _history_grew = len(engine._history) > _prefix_history_len
            _needs_rebuild = msg.sender == "用户" or _history_grew

            cycle_tail: list[dict] = []
            if content:
                cycle_tail.append({"role": "assistant", "content": content})

            _anti_repeat_tag = f"[提醒] 你（{name}）已经发表过上述观点"
            if not any(
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and _anti_repeat_tag in m["content"]
                for m in messages[-6:]
            ):
                cycle_tail.append({
                    "role": "system",
                    "content": (
                        f"{_anti_repeat_tag}。"
                        f"针对队友的新消息做出回应或补充新观点，不要重复已说的内容。"
                    ),
                })

            if msg.sender == "用户":
                cycle_tail.append({
                    "role": "user",
                    "content": f"[用户 — 最新消息]: {msg.content}",
                })
            else:
                cycle_tail.append({
                    "role": "user",
                    "content": f"[队友消息] {msg}",
                })

            if _needs_rebuild:
                _apply_live_prefix(
                    cycle_tail,
                    reason=f"wait wake from {msg.sender}",
                )
                continue

            # ── Prune conversation tail (no history rebuild needed) ──
            from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary
            from nanobot.groupchat.history.history_settings import summarize_model as _summarize_model
            _conv_keep_turns = gc_settings.get("conv_keep_turns", 3)
            dropped = await prune_conversation_tail_with_summary(
                messages, _sys_msg_count, _conv_keep_turns,
                provider=engine.provider,
                model=_summarize_model(),
                agent_name=name,
            )
            if dropped > 0:
                logger.debug(
                    "Broadcast: {} pruned {} conversation messages (kept {})",
                    name, dropped, _conv_keep_turns * 3,
                )
            messages.extend(cycle_tail)

        # ── Final completion ──
        await tracker.set_state(name, "done")
        comp = _d.completion_msg(name, round(total_latency, 1), total_iterations, all_tools_used, leader=leader_name)
        if comp:
            await engine._send(comp, progress=True)

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
            await engine._send(comp, progress=True)
        return (name, content, all_tools_used, {})

    except Exception as e:
        await tracker.set_state(name, "error", reason=str(e)[:40])
        logger.error("Broadcast: {} failed: {}", name, e)
        await engine._send(f"  ✗ {name} error: {e}", progress=True)
        log_request(engine, name, model, "broadcast",
                    error=str(e))
        return (name, None, [], {})
    finally:
        # Defensive: release any remaining pool slots held by this agent
        # (e.g. if cancelled or errored before auto-wait could release them)
        if pool:
            pool.release_unread(name)
        mailbox.mark_agent_done(name)
