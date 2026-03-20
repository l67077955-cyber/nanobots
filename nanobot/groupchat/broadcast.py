"""Broadcast execution mode for group chat.

Runs all agents concurrently with out-of-order display.
Agents can communicate with each other via chatroom_send/wait tools.
"""

from __future__ import annotations

import asyncio
import time as _time
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool


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
    mailbox.start_round(active_agents=list(agents))

    total = len(agents)

    # Announce broadcast start
    _round_t0 = _time.time()
    engine._save_event("round_start", extra={
        "round": engine._round + 1,
        "agents": list(agents),
        "mode": "broadcast",
    })
    await engine._send(_d.broadcast_start_msg(list(agents), int(global_timeout)))

    # ── Load groupchat settings ──
    _gc_settings_path = Path.home() / ".nanobot" / "groupchat_settings.json"
    _gc_defaults = {"search_initial": 1, "search_refund": 1, "allocate_timeout": 15}
    gc_settings = dict(_gc_defaults)
    if _gc_settings_path.exists():
        try:
            import json as _json
            gc_settings.update(_json.loads(_gc_settings_path.read_text()))
        except Exception:
            pass

    # ── ConversationPool: OS-style resource pool ──
    n = len(agents)
    pool_capacity = n * (n - 1)  # each agent can msg all others once
    pool = ConversationPool(capacity=pool_capacity, agents=list(agents))
    pool.ALLOCATE_TIMEOUT = float(gc_settings["allocate_timeout"])
    await engine._send(f"── threads {_d.thread_bar(0, pool_capacity)} ──")

    # ── Build per-agent tool registries with chatroom tools ──
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.base import Tool
    from nanobot.groupchat.chatroom_tools import (
        ChatroomSendTool, WaitTool, CachedSearchTool, SearchTree,
    )

    agent_tool_registries: dict[str, ToolRegistry] = {}

    # ── Shared search cache for deduplication ──
    _search_cache: dict[str, tuple[str, str]] = {}

    # ── Shared search tree (agents × initial points, refund k per search) ──
    search_tree = SearchTree(
        agents=list(agents),
        total=n * gc_settings["search_initial"],
        refund=gc_settings["search_refund"],
    )

    for name in agents:
        # Clone the engine's group tool registry and add chatroom tools
        registry = ToolRegistry()
        # Copy existing tools from default registry, wrapping web_search with cache
        for tool_name in engine.tools.tool_names:
            tool = engine.tools.get(tool_name)
            if tool:
                if tool_name == "web_search":
                    registry.register(CachedSearchTool(tool, name, _search_cache, search_tree=search_tree))
                else:
                    registry.register(tool)
        # Add chatroom tools (per-agent instances with ConversationPool)
        send_tool = ChatroomSendTool(mailbox=mailbox, agent_name=name, pool=pool)
        wait_tool = WaitTool(mailbox=mailbox, agent_name=name, pool=pool)
        wait_tool._send_tool = send_tool  # link for reply tracking
        registry.register(send_tool)
        registry.register(wait_tool)
        agent_tool_registries[name] = registry

    # ── Extract user question (for hint injection) ──
    user_question = ""
    for msg in reversed(engine._history):
        if msg.get("sender") in ("User", "user", "用户", "系统"):
            user_question = msg.get("content", "")[:300]
            break

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
        overrides = engine.prompt_builder._load_prompt_overrides("__global__")
        hint_template = overrides.get("broadcast_hint") or engine.prompt_builder.get_component_template("broadcast_hint")
        if hint_template:
            # user_question is already extracted by the planning phase (outer scope)
            hint = (
                hint_template
                .replace("{{agent_idx}}", str(agent_idx + 1))
                .replace("{{total}}", str(total))
                .replace("{{teammates}}", ", ".join(teammates))
                .replace("{{agent}}", name)
                .replace("{{user_question}}", user_question)
            )

            insert_pos = max(len(messages) - 1, 0)
            messages.insert(insert_pos, {
                "role": "system",
                "content": hint,
            })

        # ── Non-streaming display (broadcast mode) ──
        # No streaming edits — each event gets its own message.
        # This prevents messages from being swallowed by concurrent edits.
        _tool_lines: list[str] = []

        badge = f" [{agent_idx + 1}/{total}]"
        _header = f"◍ {name}{badge}: "

        # Send initial status
        await engine._send(_d.thinking_msg(name, model_short, idx=agent_idx + 1, total=total))


        async def _on_tool_start(tool_name: str, args: dict) -> None:
            if not isinstance(args, dict):
                args = {}
            # Persist tool_call event to session log
            engine._save_event("tool_call", agent=name, extra={
                "tool": tool_name,
                "args": {k: (v[:200] if isinstance(v, str) else v) for k, v in args.items()},
            })
            if tool_name == "chatroom_send":
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
                await engine._send(_d.chatroom_send_msg(name, to_str, msg_full))
            elif tool_name == "wait":
                from_who = args.get("from_agent", "")
                line = f"{name}: wait({'来自 ' + from_who if from_who else '消息'})"
                _tool_lines.append(line)
            else:
                # Show tool activity to user
                line = _d.tool_activity_msg(name, tool_name, args)
                _tool_lines.append(line)
                await engine._send(line)

        async def _on_tool_result(tool_name: str, tool_call_id: str, result: str) -> None:
            # Persist tool_result event to session log
            engine._save_event("tool_result", agent=name, extra={
                "tool": tool_name,
                "result_len": len(result) if result else 0,
                "success": not (result or "").startswith("Error:"),
            })
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
                await engine._send(_d.chatroom_wait_msg(name, result))
            # Show tool result brief for network/exec tools
            elif tool_name in ("web_search", "web_fetch", "exec") and result:
                await engine._send(_d.tool_result_brief(name, tool_name, result))

        # ── Determine tool definitions ──
        reg = agent_tool_registries[name]
        tool_defs = engine._get_agent_tools(agent_cfg, reg)
        # Always include chatroom tools for broadcast mode
        chatroom_defs = [
            t.to_schema() for t in [
                reg.get("chatroom_send"), reg.get("wait"),
            ]
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
        # ── Run tool-loop + auto-wait cycle ──
        # After tool_loop finishes, agent automatically enters wait().
        # If a teammate message arrives, inject it and re-run tool_loop.
        # Only exits when cancelled (all-agents-wait) or on error.
        from nanobot.agent.tool_loop import tool_loop

        all_tools_used: list[str] = []
        total_iterations = 0
        total_latency = 0.0
        cycle = 0
        MAX_CYCLES = 4  # Safety cap: at most 4 reactivations

        try:
            while cycle < MAX_CYCLES:
                cycle += 1

                result = await tool_loop(
                    provider=engine.provider,
                    messages=messages,
                    tool_registry=reg,
                    model=model,
                    max_tokens=engine.config.max_tokens,
                    max_iterations=8,
                    tool_defs=tool_defs if tool_defs else None,
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
                    on_content_delta=None,
                    on_content_reset=None,
                    clean_response=lambda c: engine._clean_response(c, name),
                    result_max_chars=20_000,
                )

                content = result.content or ""
                is_error = result.finish_reason == "error"
                latency = result.latency
                total_latency += latency
                total_iterations += result.iterations
                all_tools_used.extend(result.tools_used or [])

                if is_error:
                    err_short = content[:150] if content else "Unknown error"
                    await engine._send(f"  ✗ {name} failed ({latency:.1f}s): {err_short}")
                    engine._request_log.append({
                        "agent": name, "model": model,
                        "reply_len": 0, "time": engine._history[-1]["content"][:50] if engine._history else "",
                        "mode": "broadcast", "error": err_short,
                        "iterations": total_iterations, "latency": total_latency,
                    })
                    return (name, None, [], {})

                # Record final text in history (not displayed)
                if content:
                    engine._add_message(name, content)

                # ── Auto-wait: enter idle state ──
                # If agent never used chatroom_send, auto-share its findings
                if content and "chatroom_send" not in (result.tools_used or []):
                    snippet = content[:500]
                    mailbox.send(name, ["All"], snippet)
                    logger.info("Broadcast: auto-shared {} findings ({} chars)", name, len(snippet))

                # Now wait for teammate messages
                logger.info("Broadcast: {} entering auto-wait (cycle {})", name, cycle)
                msg = await mailbox.wait(name, timeout=60)

                if msg is None:
                    # Timeout — no one talking to us, we're done
                    logger.info("Broadcast: {} auto-wait timeout, exiting", name)
                    break

                # Got a message! Inject it and re-run tool_loop
                logger.info("Broadcast: {} reactivated by {}: {}", name, msg.sender, msg.content[:60])
                await engine._send(_d.chatroom_wait_msg(name, str(msg)))
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
            comp = _d.completion_msg(name, round(total_latency, 1), total_iterations, all_tools_used)
            if comp:
                await engine._send(comp)

            engine._request_log.append({
                "agent": name, "model": model,
                "reply_len": len(content) if content else 0, "time": _time.strftime("%H:%M:%S"),
                "mode": "broadcast", "tools": all_tools_used,
                "iterations": total_iterations, "latency": round(total_latency, 1),
            })
            return (name, content, all_tools_used, {})

        except asyncio.CancelledError:
            # Cancelled by sentinel (all-agents-waiting) — normal exit
            comp = _d.completion_msg(name, round(total_latency, 1), total_iterations, all_tools_used)
            if comp:
                await engine._send(comp)
            return (name, content if 'content' in dir() else "", all_tools_used, {})

        except Exception as e:
            logger.error("Broadcast: {} failed: {}", name, e)
            await engine._send(f"  ✗ {name} error: {e}")
            engine._request_log.append({
                "agent": name, "model": model,
                "reply_len": 0, "time": _time.strftime("%H:%M:%S"),
                "mode": "broadcast", "error": str(e),
            })
            return (name, None, [], {})
        finally:
            mailbox.mark_agent_done(name)

    # ── Launch all agents concurrently ──
    tasks = {
        asyncio.create_task(_run_one(name, idx)): name
        for idx, name in enumerate(agents)
    }

    results: list[tuple[str, str | None, list[str]]] = []
    completed = 0

    try:
        # ── User interjection listener ──
        # Allows user to send messages mid-round via input_queue,
        # which get delivered to all agents via mailbox.
        # Allocates pool slots (n = agent count) to ensure fair resource usage.
        _user_listener_running = True

        async def _user_listener() -> None:
            while _user_listener_running:
                try:
                    msg = await asyncio.wait_for(engine._input_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg == "__SUMMARY__":
                    continue  # skip control messages

                # User messages block agents and wait for n slots (no timeout).
                all_agent_names = list(agents)
                await pool.allocate_user(all_agent_names)

                # Deliver to all agents via mailbox
                mailbox.create("用户")  # ensure sender mailbox exists
                mailbox.send("用户", ["All"], msg)
                engine._add_message("用户", msg)
                await engine._send(
                    f"─── User ───\n{msg}\n"
                    f"  {_d.thread_bar(pool.used, pool.capacity)}"
                )
                logger.info("Broadcast: user interjected: {}", msg[:60])

        user_task = asyncio.create_task(_user_listener())

        # Also watch for all-agents-waiting (natural conversation end)
        async def _watch_all_waiting() -> None:
            await mailbox.all_waiting_event.wait()

        sentinel = asyncio.create_task(_watch_all_waiting())
        all_tasks = set(tasks.keys()) | {sentinel}

        while not all(t.done() for t in tasks.keys()):
            # Wait for any task to complete (agent done or all-waiting)
            done_set, _ = await asyncio.wait(
                [t for t in all_tasks if not t.done()],
                timeout=global_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done_set:
                # Global timeout
                break

            for t in done_set:
                if t is sentinel:
                    # All agents are waiting simultaneously — end conversation
                    logger.info("Broadcast: all agents waiting, ending round")
                    await engine._send("━━ all agents idle — round complete ━━")
                    for task_obj in tasks:
                        if not task_obj.done():
                            task_obj.cancel()
                    break
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
            break  # broke from inner loop (sentinel fired)

        # Cancel sentinel if still running
        if not sentinel.done():
            sentinel.cancel()

        # Stop user listener
        _user_listener_running = False
        if not user_task.done():
            user_task.cancel()

        # Cancel any remaining agent tasks
        for task_obj in tasks:
            if not task_obj.done():
                name = tasks[task_obj]
                task_obj.cancel()
                logger.warning("Broadcast: {} cancelled", name)
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
    chain = _d.chat_chain_summary(mailbox.history)
    if chain:
        await engine._send(chain)

    # Clean up queues (history preserved for synthesis & test harness)
    mailbox.clear()

    # ── Phase 2: Leader final reply ──
    # If a leader is set, they get to speak last, seeing the full history
    # (including all other agents' responses). No synthesis instructions injected.
    leader_name = engine._leader
    if leader_name and leader_name in agents:
        model_short = engine.registry.get(leader_name, {}).get("model", "?").split("/")[-1]
        await engine._send(f"👑 {leader_name} ({model_short}) 正在发表最终看法...")
        try:
            await engine._agent_speak(leader_name)
        except Exception as e:
            logger.error("Broadcast leader final reply: {} failed: {}", leader_name, e)
            await engine._send(f"✗ {leader_name} failed: {e}")

    return [(name, content) for name, content, _ in results]

