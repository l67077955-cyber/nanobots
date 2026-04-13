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


# ── Tool-name → status state mapping ─────────────────────────
_TOOL_STATE_MAP: dict[str, str] = {
    "web_search": "searching",
    "web_fetch": "fetching",
    "exec": "executing",
    "read_file": "reading",
    "write_file": "writing",
    "edit_file": "writing",
    "list_dir": "reading",
    "chatroom_send": "sending",
    "wait": "waiting",
    "interrupted": "interrupted",
}


class AgentStatusTracker:
    """Live status dashboard — one Telegram message edited in-place.

    Thread-safe for concurrent agent coroutines on the same event loop.
    Gracefully degrades to no-op when edit_fn is unavailable (e.g. CLI).
    """

    EDIT_INTERVAL = 0.8  # seconds — matches StreamingDisplay throttle

    def __init__(
        self,
        agents: list[str],
        leader: str | None,
        edit_fn: Callable[[int, str], Awaitable[None]] | None,
        send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None,
    ):
        self._agents = list(agents)
        self._leader = leader
        self._edit_fn = edit_fn
        self._send_and_get_id = send_and_get_id_fn
        self._msg_id: int | None = None
        self._states: dict[str, str] = {a: "thinking" for a in agents}
        self._details: dict[str, str] = {a: "" for a in agents}
        self._reasons: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._last_edit: float = 0.0
        self._dirty = False

    async def create_panel(self) -> None:
        """Send the initial status panel message and store its ID."""
        if not self._send_and_get_id:
            return
        try:
            text = self._render()
            self._msg_id = await self._send_and_get_id(text)
        except Exception as e:
            logger.warning("StatusTracker: create_panel failed: {}", e)

    async def set_state(
        self,
        agent: str,
        state: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        """Update an agent's state and trigger a throttled panel refresh."""
        async with self._lock:
            if agent not in self._states:
                return
            self._states[agent] = state
            self._details[agent] = detail
            if reason:
                self._reasons[agent] = reason
            elif state not in ("blocked", "error", "done", "cancelled"):
                self._reasons.pop(agent, None)
            self._dirty = True
        await self._maybe_refresh()

    async def _maybe_refresh(self) -> None:
        """Edit the panel message if dirty and throttle interval has passed."""
        if not self._msg_id or not self._edit_fn or not self._dirty:
            return
        import time
        now = time.time()
        if now - self._last_edit < self.EDIT_INTERVAL:
            return
        async with self._lock:
            if not self._dirty:
                return
            text = self._render()
            self._dirty = False
        self._last_edit = now
        try:
            await self._edit_fn(self._msg_id, text)
        except Exception as e:
            logger.debug("StatusTracker: edit failed: {}", e)

    def _render(self) -> str:
        """Build the panel text using the display module."""
        return _d.status_panel(
            self._agents, self._states, self._details,
            self._reasons, leader=self._leader,
        )

    async def finalize(self) -> None:
        """Force a final panel edit regardless of throttle."""
        if not self._msg_id or not self._edit_fn:
            return
        async with self._lock:
            text = self._render()
            self._dirty = False
        try:
            await self._edit_fn(self._msg_id, text)
        except Exception:
            pass

    def add_agent(self, name: str) -> None:
        """Register a new agent that joined mid-round."""
        if name not in self._states:
            self._agents.append(name)
            self._states[name] = "thinking"
            self._details[name] = ""
            self._dirty = True


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

    # Lazy-connect MCP servers before building tool registries
    if hasattr(engine, '_connect_mcp'):
        await engine._connect_mcp()

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

    # ── Live status dashboard ──
    tracker = AgentStatusTracker(
        agents=exec_agents,
        leader=leader_name,
        edit_fn=getattr(engine, '_edit_fn', None),
        send_and_get_id_fn=getattr(engine, '_send_and_get_id_fn', None),
    )
    await tracker.create_panel()

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

    # ── Shared Memory Palace (all agents; persists across rounds + sessions) ──
    from nanobot.agent.tools.memory_palace import MemoryPalaceTool
    _palace_path = gc_settings.get("memory_palace_path", "./memory_palace")
    memory_palace = MemoryPalaceTool(storage_path=_palace_path)

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
        registry.register(memory_palace)  # shared palace — all agents can read/write
        agent_tool_registries[name] = registry

    # ── Leader-specific tools: manage_agent + end_discussion + transfer_credits + clear_context ──
    leader_end_event = asyncio.Event()
    _leader_agent_tasks: dict = {}  # populated after tasks are created

    # ── 关键修复：tasks / all_tasks 必须在 spawn_fn 之前创建 ──
    tasks: dict[asyncio.Task, str] = {}
    all_tasks: set[asyncio.Task] = set()

    def _spawn_agent_task(name: str, idx: int) -> asyncio.Task:
        """Re-spawn a single agent task (used by ManageAgentTool.restart)."""
        task = asyncio.create_task(_run_one(name, idx))
        tasks[task] = name
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
            spawn_fn=_spawn_agent_task,   # 现在安全
        )
        end_tool = EndDiscussionTool(end_event=leader_end_event, engine=engine)
        transfer_tool = TransferCreditsTool(search_pool=search_pool, engine=engine)
        clear_ctx_tool = ClearContextTool(
            engine=engine,
            mailbox=mailbox,
            exec_agents=non_leader_agents,
            leader_name=leader_name,
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
        import time as _t
        _cycle_t0 = _t.time()

        if name not in engine.registry:
            return (name, None, [], {})

        agent_cfg = engine.registry[name]
        model = agent_cfg["model"]  # initial; re-read each cycle below
        model_short = model.split("/")[-1]
        # In broadcast mode each agent only sees its own prior turns in history.
        # User/system messages are always kept; other agents' verbose outputs
        # are filtered out to reduce noise and context bloat.
        messages = engine._build_agent_prompt(name, relevant_agents=[name])

        is_leader = (name == leader_name)

        # ── Inject broadcast coordination hint from template ──
        teammates = [a for a in agents if a != name]
        # Load from override system (editable via /prompt), fallback to default
        # Removed stale prompt_overrides.json lookup; .md files are the source of truth.

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
                f"## 搜索额度管理\n"
                f"每个 agent 有独立的搜索额度（{search_pool.status()}）。\n"
                f"没有 web_search 的 agent 的额度闲置，你可以用 transfer_credits 把他们的额度\n"
                f"划拨给有搜索能力的 agent（包括你自己）。\n\n"
                f"## 工作流程\n"
                f"1. 先用 memory_palace(action='search') 检索是否有相关历史记忆\n"
                f"2. 分析问题，决定如何分工\n"
                f"3. 用 chatroom_send 给队友分配具体任务（写清楚要做什么）\n"
                f"   ⚠️ 只分配队友有工具能力完成的任务！无 web_search 的队友不要让他搜索\n"
                f"4. 用 wait() 等待队友回复结果\n"
                f"5. 根据结果：追加任务 / 纠正方向 / 自己补充搜索\n"
                f"6. 信息充分后，先完成以下两步，再调用 end_discussion()：\n"
                f"   a. 在最终文字回复中整合所有发现，给出完整答案\n"
                f"   b. 用 memory_palace(action='store') 将关键结论、发现写入记忆宫殿\n"
                f"      示例: memory_palace(action='store', content='用户偏好：...', wing='用户', hall='偏好', room='main')\n"
                f"7. 完成记忆存入后，调用 end_discussion() 结束任务\n\n"
                f"## 关键规则\n"
                f"- 发现队友空转或无法完成任务时：果断 end_discussion\n"
                f"- 可以一次给多个队友同时发任务（并行工作）\n"
                f"- 你的最终文字回复就是给用户的答案，要完整、结构化\n"
                f"- ⚠️ 如果你打算自己做搜索/验证，必须先完成工具调用，再调用 end_discussion。\n"
                f"  end_discussion 一旦触发无法撤销，之后再说'我来搜索'只是文字，不会执行。\n"
                f"- ⚠️ 原假设被否证时，不要立即结束。应转向：'那么最近的可验证链条是什么？'\n"
                f"  继续搜索直到能给出正面结论（即使度数更高），而不是仅报告'不成立'。\n"
                f"- ⚠️ 禁止在未存记忆的情况下调用 end_discussion。存记忆 → end_discussion 是强制顺序。\n"
            )
            messages.insert(max(len(messages) - 1, 0), {
                "role": "system",
                "content": leader_hint,
            })
        else:
            # ── Non-leader: standard broadcast hint + wait for leader ──
            hint_template = engine.prompt_builder.get_component_template("broadcast_hint")
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
        # Shared state between _on_tool_start and _on_tool_result for chatroom_send args
        _last_chatroom_send_to: list[str] = []

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

        async def _on_tool_start(tool_name: str, args: dict, **_kw) -> None:
            if not isinstance(args, dict):
                args = {}
            # ── Update status dashboard ──
            _st = _TOOL_STATE_MAP.get(tool_name, "thinking")
            _dt = ""
            if tool_name == "web_search":
                _dt = (args.get("query") or args.get("queries", ""))
                if isinstance(_dt, list):
                    _dt = ", ".join(_dt)
            elif tool_name == "web_fetch":
                _dt = (args.get("url", "") or "")[:35]
            elif tool_name == "exec":
                _dt = (args.get("command", "") or "")[:25]
            elif tool_name in ("read_file", "write_file", "edit_file"):
                _dt = (args.get("path", "") or "").split("/")[-1]
            elif tool_name == "chatroom_send":
                _to = args.get("to", "?")
                _dt = ", ".join(_to) if isinstance(_to, list) else str(_to)
            await tracker.set_state(name, _st, detail=str(_dt)[:30])
            # Persist tool_call event to session log
            engine._save_event("tool_call", agent=name, extra={
                "tool": tool_name,
                "args": {k: (v if isinstance(v, str) else v) for k, v in args.items()},
            })
            # Full args logging to server log
            logger.info(
                "broadcast [{}] tool_call: {}({})",
                name, tool_name, _json.dumps(args, ensure_ascii=False)[:300],
            )
            if tool_name == "chatroom_send":
                # Flush any buffered searches before showing chatroom_send
                await _flush_searches()
                # Normalize `to` → to_list (handles str / list / None uniformly)
                raw_to = args.get("to", "?")
                if isinstance(raw_to, list):
                    to_list = [str(t).strip() for t in raw_to if t]
                elif isinstance(raw_to, str):
                    to_list = [s for s in [raw_to.strip()] if s]
                else:
                    to_list = []
                if not to_list:
                    to_list = ["?"]
                # Stash to-list for _on_tool_result to check (Leader interrupt logic)
                _last_chatroom_send_to.clear()
                _last_chatroom_send_to.extend(to_list)
                msg_full = (args.get("message", "") or "")
                to_str = ", ".join(to_list)
                # Calculate cost for display (use normalized to_list)
                to_lower = [t.lower() for t in to_list]
                if "all" in to_lower:
                    cost = len([a for a in agents if a != name])
                else:
                    cost = len(to_list)
                line = f"{name}: chatroom_send({to_str}) [cost={cost}]"
                _tool_lines.append(line)
                # Build stats suffix: token + latency
                elapsed = _t.time() - _cycle_t0
                tok_t = _cycle_usage.get("total_tokens", 0)
                stats_suffix = ""
                if tok_t > 0:
                    p = _cycle_usage.get("prompt_tokens", 0)
                    c = _cycle_usage.get("completion_tokens", 0)
                    stats_suffix = "\n" + _d.format_token_stats(p, c, elapsed=elapsed)
                await engine._send(_d.chatroom_send_msg(name, to_str, msg_full + stats_suffix, leader=leader_name))
            elif tool_name == "wait":
                await _flush_searches()
                from_who = args.get("from_agent", "")
                line = f"{name}: wait({'来自 ' + from_who if from_who else '所有人'})"
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
            # ── Update status dashboard (blocked detection) ──
            _r = result or ""
            if tool_name == "chatroom_send" and "BLOCKED:" in _r:
                await tracker.set_state(name, "blocked", reason="pool full")
            elif tool_name == "chatroom_send" and "你已发过 1 条消息" in _r:
                await tracker.set_state(name, "blocked", reason="leader gate")
            elif tool_name == "web_search" and "BLOCKED:" in _r and "额度" in _r:
                await tracker.set_state(name, "blocked", reason="no credits")
            elif tool_name == "web_search" and "BLOCKED:" in _r and "本轮已搜索" in _r:
                await tracker.set_state(name, "blocked", reason="cycle limit")
            else:
                await tracker.set_state(name, "thinking")
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
                elif leader_name and leader_name != name:
                    # ── Leader 实时打断特权 ──
                    # 消息成功投递且目标包含 Leader 或 All → 立即打断 Leader
                    # 让 Leader 实时感知队友汇报，不必等当前 tool_loop 跑完
                    _targets_lower = [t.lower() for t in _last_chatroom_send_to]
                    if leader_name.lower() in _targets_lower or "all" in _targets_lower:
                        _interrupted = mailbox.interrupt_busy_agents(name)
                        if _interrupted > 0:
                            logger.info(
                                "Broadcast: 队友 {} 消息触发 Leader({}) 实时打断",
                                name, leader_name,
                            )
                            await engine._send(
                                f"⚡ {leader_name} 被 {name} 的汇报实时打断..."
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
        broadcast_tool_names = ["chatroom_send", "wait", "memory_palace"]
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
        # Only exits when cancelled by leader end_discussion, /stop, or on error.
        from nanobot.agent.tool_loop import tool_loop

        # Load configurable result_max_chars for broadcast mode
        try:
            from nanobot.groupchat.history_settings import broadcast_result_max_chars
            _broadcast_result_max = broadcast_result_max_chars()
        except Exception:
            _broadcast_result_max = 20_000

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
        _sys_msg_count = len(messages)

        # ── Forced interrupt: get this agent's interrupt event from mailbox ──
        _interrupt_event = mailbox.get_interrupt_event(name)
        # Tracks how many timeout-recovery attempts this agent has made.
        # Hard cap at 1 to prevent recovery loops.
        _timeout_recovery_count = 0

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
                            from nanobot.agent.tool_loop import tool_loop as _final_loop
                            _r = await _final_loop(
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
                # Respect /stop — exit immediately if engine is no longer running
                if not engine._running:
                    logger.info("Broadcast: {} exiting — engine stopped", name)
                    break
                cycle += 1
                # Re-read model from registry each cycle so mid-round changes take effect
                _live_cfg = engine.registry.get(name, agent_cfg)
                model = _live_cfg.get("model", model)
                _cycle_t0 = _t.time()
                _cycle_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

                async def _on_iter_usage(usage: dict) -> None:
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        _cycle_usage[k] += usage.get(k, 0)

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
                        result_max_chars=_broadcast_result_max,
                        call_timeout=float(gc_settings.get("call_timeout", 90)) or None,
                        interrupt_event=_interrupt_event,
                    )
                finally:
                    # Always mark idle when tool_loop exits (interrupt, stop, normal, error)
                    mailbox.mark_idle(name)


                # Flush any remaining buffered search lines
                await _flush_searches()

                content = result.content or ""
                is_error = result.finish_reason == "error"
                is_timeout = result.finish_reason == "timeout"
                is_interrupted = result.finish_reason == "interrupted"
                latency = result.latency
                total_latency += latency
                total_iterations += result.iterations
                all_tools_used.extend(result.tools_used or [])

                if is_error or is_timeout:
                    if is_timeout:
                        _base_timeout = gc_settings.get(
                            "leader_call_timeout" if is_leader else "call_timeout",
                            180 if is_leader else 90,
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
                                    engine._add_message(name, content)
                                    search_pool.on_output(name)
                                    mailbox.send(name, ["All"], content[:300])
                                    await engine._send(
                                        _d.chatroom_send_msg(
                                            name, "重试输出", content, max_len=1000, leader=leader_name
                                        )
                                    )
                                    logger.info(
                                        "Broadcast: {} retry succeeded ({:.1f}s): {}",
                                        name, _r.latency, content[:80],
                                    )
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

                    log_request(engine, name, model, "broadcast",
                                error=err_short, iterations=total_iterations,
                                latency=total_latency)

                    # Broadcast the error so teammates stop waiting for this agent
                    _alert = f"⚠️ [System Alert] {'LLM call timed out' if is_timeout else 'Fatal error'}. Details: {err_short}"
                    engine._add_message(name, _alert)
                    mailbox.send(name, ["All"], _alert)

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

                # ── Handle forced interrupt ──
                if is_interrupted:
                    # Clear the event so it can be set again by a future message
                    _interrupt_event.clear()
                    # (agent is already idle — the try/finally around tool_loop handled it)

                    # Read the pending interrupt message from mailbox (non-blocking fast-path)
                    _intr_q = mailbox._queues.get(name)
                    _intr_msg = None
                    if _intr_q and not _intr_q.empty():
                        try:
                            _intr_msg = _intr_q.get_nowait()
                        except Exception:
                            pass

                    # UI: show who interrupted whom, with distinct label for user vs agent
                    _sender_name = _intr_msg.sender if _intr_msg else "teammate"
                    await tracker.set_state(name, "interrupted", detail=f"from {_sender_name}")
                    if _sender_name == "用户":
                        await engine._send(
                            f"⚡ {name} 被【用户消息】打断，正在立即响应..."
                        )
                    elif is_leader and _sender_name != "用户":
                        await engine._send(
                            f"⚡ {name}（Leader）被队友 **{_sender_name}** 汇报实时打断，正在响应..."
                        )
                    else:
                        await engine._send(
                            f"⚡ {name} 被 {_sender_name} 的消息打断，正在立即响应..."
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

                    # Prune conversation tail before re-entry (same as normal reactivation)
                    _CONV_KEEP_TURNS = 6
                    _max_conv = _CONV_KEEP_TURNS * 3
                    conv_msgs = messages[_sys_msg_count:]
                    if len(conv_msgs) > _max_conv:
                        dropped = len(conv_msgs) - _max_conv
                        messages[_sys_msg_count:] = conv_msgs[-_max_conv:]
                        logger.debug(
                            "Broadcast: {} pruned {} messages after interrupt",
                            name, dropped,
                        )

                    # Inject any partial content so LLM knows what it said already
                    if content:
                        messages.append({"role": "assistant", "content": content})

                    # Inject the interrupt message as a "user" message from the sender
                    if _intr_msg:
                        messages.append({
                            "role": "user",
                            "content": f"[{_intr_msg.sender}]: {_intr_msg.content}",
                        })
                    else:
                        # Fallback: no message in queue (already consumed by auto-wait?)
                        messages.append({
                            "role": "system",
                            "content": f"[打断通知] 你的执行被中断，请立即总结当前进展并响应队友的最新需求。",
                        })

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

                # ── 关键修复3：Leader end_discussion 但没有文字输出时强制合成 ──
                if is_leader and "end_discussion" in (result.tools_used or []) and not content:
                    logger.warning(
                        "Broadcast: leader {} called end_discussion without text (cycle {}), forcing synthesis",
                        name, cycle,
                    )
                    messages.append({
                        "role": "system",
                        "content": (
                            "[⚠️ 你调用了 end_discussion，但还没有给出最终答案！]\n"
                            "请立即整合所有队友的发现，给出完整、结构化的最终回复给用户。\n"
                            "这是用户唯一能看到的内容，禁止再调用任何工具，直接输出文字。"
                        ),
                    })
                    continue  # 重新生成最终答案

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
                if is_leader and "end_discussion" in (result.tools_used or []):
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
                    # No message — check if engine stopped, otherwise keep waiting
                    if not engine._running:
                        await tracker.set_state(name, "done", reason="engine stopped")
                        logger.info("Broadcast: {} wait returned None, engine stopped, exiting", name)
                        break
                    # Leader fallback: if no text was produced, force synthesis
                    if is_leader and not content:
                        logger.warning(
                            "Broadcast: leader {} wait timeout with no text (cycle {}), forcing synthesis",
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
                    # Non-leader: keep waiting
                    logger.info("Broadcast: {} wait timeout, retrying wait", name)
                    continue

                # Got a message! Inject it and re-run tool_loop
                # But first check if /stop was issued while we were waiting
                if not engine._running:
                    logger.info("Broadcast: {} exiting after wait — engine stopped", name)
                    break
                logger.info("Broadcast: {} reactivated by {}: {}", name, msg.sender, msg.content[:60])
                await tracker.set_state(name, "thinking")
                await engine._send(_d.chatroom_wait_msg(name, str(msg), leader=leader_name))

                # ── Prune conversation tail to prevent unbounded growth ──
                # Keep system-prompt prefix intact; only retain the last
                # _CONV_KEEP_TURNS conversation turns (3 msgs per turn).
                _CONV_KEEP_TURNS = 6  # keep last 6 reactivation cycles worth of msgs
                _max_conv = _CONV_KEEP_TURNS * 3
                conv_msgs = messages[_sys_msg_count:]
                if len(conv_msgs) > _max_conv:
                    dropped = len(conv_msgs) - _max_conv
                    messages[_sys_msg_count:] = conv_msgs[-_max_conv:]
                    logger.debug(
                        "Broadcast: {} pruned {} conversation messages (kept {})",
                        name, dropped, _max_conv,
                    )

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

                all_agent_names = list(agents)
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
                    f"  {_d.thread_bar(pool.used, pool.capacity)}"
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
                    await engine._send("━━ Leader 结束讨论 — entering synthesis ━━")
                    for task_obj, task_name in tasks.items():
                        if not task_obj.done() and task_name != leader_name:
                            await tracker.set_state(task_name, "cancelled", reason="leader ended")
                            task_obj.cancel()
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

    # ── Restore original settings (session-scoped overrides) ──
    if _original_settings:
        for name, orig in _original_settings.items():
            cfg = engine.registry.get(name)
            if cfg and orig.get("tools"):
                cfg["tools"] = orig["tools"]
        logger.info("Broadcast: restored original agent settings")

    return [(name, content) for name, content, _ in results]

