"""Broadcast 模式 — Leader 驱动的多 Agent 协作。

架构：
    broadcast_round()  →  BroadcastCoordinator
        .setup()       →  初始化工具、state_bus、创建 runner
        .run()         →  启动 leader + state_poller + user_listener
        .synthesize()  →  无 leader 时自动总结

核心设计（纯变量驱动）：
    - 系统是 "哑执行器" — 通过 poll_changes() 检测 state.yaml 变量变化并执行
    - Leader 通过 edit_file 直接修改 state.yaml 变量来控制一切：
        · 新增 agent block → 启动 agent
        · 删除 agent block → 移除 agent
        · 改 state: paused → 暂停 agent
        · 改 muted: true → 禁言
    - 没有 commands，没有函数调用，没有中间层

⚠️ 关键约束（agent 修改代码时注意）：
    1. broadcast_round() 是公共 API，签名不可改
    2. state_bus 方法名不可改（set_agent_activity, update_session 等）
    3. poll_changes() 返回的 type 名字不可改
"""

from __future__ import annotations

import asyncio
import copy
import time as _time
from typing import Any

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.agent_runner import AgentRunner, AgentResult, AgentState
from nanobot.groupchat.mailbox import MailboxHub
from nanobot.groupchat.state_bus import FileStateBus
from nanobot.groupchat.utils import log_request


def _extract_user_question(history: list[dict[str, str]]) -> str:
    """取最近一条用户消息。"""
    for msg in reversed(history):
        if msg.get("sender") in ("User", "user", "用户", "系统"):
            return msg.get("content", "")[:300]
    return ""


# ══════════════════════════════════════════════════════════════
# BroadcastCoordinator
# ══════════════════════════════════════════════════════════════

class BroadcastCoordinator:
    """协调广播轮次：setup → run → synthesize。

    Leader 通过直接修改 state.yaml 变量控制一切。
    系统通过 poll_changes() 检测变化并执行。
    """

    def __init__(self, agents: list[str], engine: Any, mailbox: MailboxHub, global_timeout: float = 200.0):
        self.agents = list(agents)
        self.engine = engine
        self.mailbox = mailbox
        self.global_timeout = global_timeout

        # Leader 检测
        self.leader_name = engine._leader if hasattr(engine, '_leader') else None
        if self.leader_name and self.leader_name not in agents:
            self.leader_name = None

        self.exec_agents = list(agents)
        self.total = len(self.exec_agents)

        # 运行状态
        self.leader_end_event = asyncio.Event()
        self.runners: dict[str, AgentRunner] = {}
        self.results: list[AgentResult] = []
        self._original_settings: dict[str, dict] = {}
        self._agent_tool_registries: dict[str, Any] = {}
        self._agent_tasks: dict[asyncio.Task, str] = {}
        self._round_t0 = 0.0
        self._user_question = ""
        self.state_bus: FileStateBus | None = None

    # ── Phase 1: Setup ──────────────────────────────────────

    def setup(self) -> None:
        """初始化工具、state_bus、创建 runner。"""
        self._round_t0 = _time.time()
        self._user_question = _extract_user_question(self.engine._history)

        # 保存原始设置以便还原
        if self.leader_name:
            for name in self.agents:
                cfg = self.engine.registry.get(name, {})
                self._original_settings[name] = {"tools": copy.deepcopy(cfg.get("tools", {}))}

        self._setup_state_bus()
        self._setup_tools()
        self._setup_runners()

    def _setup_state_bus(self) -> None:
        """初始化 state_bus — 必须复用 engine 的单例。"""
        if hasattr(self.engine, '_state_bus') and self.engine._state_bus:
            self.state_bus = self.engine._state_bus
        else:
            session_dir = self.engine._session_dir
            if not session_dir:
                return
            self.state_bus = FileStateBus(session_dir)
            self.engine._state_bus = self.state_bus

        self.state_bus.init_session(
            leader=self.leader_name,
            topic=getattr(self.engine, '_topic', '') or '',
            round_num=self.engine._round + 1,
            active_agents=list(self.exec_agents),
        )

    def _setup_tools(self) -> None:
        """为所有 agent 准备工具注册表。"""
        for name in self.exec_agents:
            self._build_agent_tools(name)

    def _build_agent_tools(self, name: str) -> None:
        """为单个 agent 构建工具注册表。"""
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.groupchat.chatroom_tools import ChatroomSendTool, WaitTool

        base_reg = self.engine._get_agent_registry(name)
        registry = ToolRegistry()

        for tool_name in base_reg.tool_names:
            tool = base_reg.get(tool_name)
            if tool and tool_name not in ("chatroom_send", "wait"):
                registry.register(tool)

        registry.register(ChatroomSendTool(mailbox=self.mailbox, agent_name=name))
        registry.register(WaitTool(mailbox=self.mailbox, agent_name=name))
        self._agent_tool_registries[name] = registry

    def _build_runner(self, name: str, messages: list[dict] | None = None) -> AgentRunner | None:
        """为单个 agent 创建 runner。"""
        if name not in self.engine.registry:
            return None

        agent_cfg = self.engine.registry[name]
        idx = self.exec_agents.index(name) if name in self.exec_agents else 0

        if messages is None:
            messages = self.engine.prompt_builder.build_broadcast_prompt(
                name, engine=self.engine, agents=self.exec_agents,
                user_question=self._user_question, leader_name=self.leader_name,
                agent_idx=idx, total=self.total,
            )

        if name not in self._agent_tool_registries:
            self._build_agent_tools(name)

        reg = self._agent_tool_registries[name]
        tool_defs = self.engine._get_agent_tools(agent_cfg, reg)
        for tn in ("chatroom_send", "wait"):
            t = reg.get(tn)
            if t:
                schema = t.to_schema()
                if not tool_defs or schema["function"]["name"] not in {d["function"]["name"] for d in tool_defs}:
                    tool_defs = (tool_defs or []) + [schema]

        runner = AgentRunner(
            name, idx, self.total,
            engine=self.engine, mailbox=self.mailbox,
            tool_registry=reg, tool_defs=tool_defs, messages=messages,
            model=agent_cfg["model"], is_leader=(name == self.leader_name),
            state_bus=self.state_bus,
        )
        self.runners[name] = runner
        return runner

    def _setup_runners(self) -> None:
        """Create AgentRunner instances for all agents."""
        for name in self.exec_agents:
            if name in self.engine.registry:
                self._build_runner(name)

    # ── Phase 2: Run ────────────────────────────────────────

    async def run(self) -> None:
        """启动 agent tasks + state poller + user listener。"""

        await self.engine._send(_d.broadcast_start_msg(
            self.agents, int(self.global_timeout), leader=self.leader_name,
        ))

        # 初始化 mailbox
        for name in self.exec_agents:
            self.mailbox.create(name)
        self.mailbox.start_round(active_agents=list(self.exec_agents))
        self.mailbox._state_bus = self.state_bus

        # 启动 runner tasks
        tasks: dict[asyncio.Task, str] = {}
        for name, runner in self.runners.items():
            tasks[asyncio.create_task(runner.run())] = name
        self._agent_tasks = tasks

        # 用户消息监听
        _running = True

        async def _user_listener() -> None:
            while _running:
                try:
                    msg = await asyncio.wait_for(self.engine._input_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg == "__SUMMARY__":
                    continue
                self.mailbox.create("用户")
                self.mailbox.send("用户", ["All"], msg)
                self.engine._add_message("用户", msg)
                await self.engine._send(f"── User ──\n{msg}")

        # ⚠️ 核心机制 — 纯变量驱动的 state poller
        async def _state_poller() -> None:
            """每 2 秒检测 state.yaml 变量变化。"""
            while _running:
                await asyncio.sleep(2.0)
                if not self.state_bus:
                    continue
                try:
                    changes = self.state_bus.poll_changes()
                    for change in changes:
                        await self._handle_change(change)
                except Exception as e:
                    logger.warning("State poller error: {}", e)

        # Leader 结束信号
        async def _watch_leader_end() -> None:
            await self.leader_end_event.wait()

        user_task = asyncio.create_task(_user_listener())
        leader_sentinel = asyncio.create_task(_watch_leader_end())
        poller_task = asyncio.create_task(_state_poller())

        # 监控循环
        SAFETY_LIMIT = 600
        try:
            while not all(t.done() for t in self._agent_tasks):
                monitored = [t for t in (set(self._agent_tasks) | {leader_sentinel}) if not t.done()]
                if not monitored:
                    break
                done_set, _ = await asyncio.wait(monitored, timeout=SAFETY_LIMIT, return_when=asyncio.FIRST_COMPLETED)

                if not done_set:
                    for t in self._agent_tasks:
                        if not t.done():
                            t.cancel()
                    break

                for t in done_set:
                    if t is leader_sentinel:
                        await self.engine._send("━━ Leader 结束讨论 — entering synthesis ━━")
                        for t2 in self._agent_tasks:
                            if not t2.done():
                                t2.cancel()
                        break
                    elif t in self._agent_tasks:
                        try:
                            self.results.append(t.result())
                        except Exception as e:
                            logger.error("Agent task error: {}", e)
                else:
                    continue
                break  # leader_sentinel triggered

        except Exception as e:
            logger.error("Broadcast run error: {}", e)

        # 清理
        _running = False
        for t in [user_task, poller_task, leader_sentinel] + list(self._agent_tasks):
            if not t.done():
                t.cancel()

        # 收集剩余结果
        for t, name in self._agent_tasks.items():
            if t.done() and not any(r.name == name for r in self.results):
                try:
                    self.results.append(t.result())
                except Exception:
                    pass

        await self.engine._send(_d.broadcast_complete_msg(len(self.results), self.total, len(self.mailbox.history)))
        self.mailbox.clear()
        if self.state_bus:
            self.state_bus.update_session(round=self.engine._round + 1)

    # ── Phase 3: Synthesize ─────────────────────────────────

    async def synthesize(self) -> None:
        """无 leader 时自动总结；有 leader 时 leader 已经总结过了。"""
        if not self.leader_name or self.leader_name not in self.agents:
            from nanobot.groupchat.run_loop import generate_summary
            await generate_summary(self.engine)

        # 还原原始设置
        if self._original_settings:
            for name, orig in self._original_settings.items():
                cfg = self.engine.registry.get(name)
                if cfg and orig.get("tools"):
                    cfg["tools"] = orig["tools"]

    def get_results(self) -> list[tuple[str, str | None]]:
        return [(r.name, r.content) for r in self.results]

    # ── Handle Changes (变量变化处理) ─────────────────────────
    # 系统检测到变量变化后的执行逻辑

    async def _handle_change(self, change: dict) -> None:
        """处理一条 state.yaml 变量变化。"""
        change_type = change.get("type", "")

        if change_type == "agent_added":
            # Leader 新增了一个 agent block → 启动 agent
            raw_name = change["name"]
            # Case-insensitive registry lookup (leader 可能写 "nanobot" 而注册表是 "Nanobot")
            resolved = self.engine._resolve_agent_name(raw_name)
            if not resolved:
                logger.warning("agent_added: '{}' not in registry, skipping", raw_name)
                return
            name = resolved
            if self._is_running(name):
                return
            # 加入 engine._active_agents（如果还不在的话）
            if name not in self.engine._active_agents:
                self.engine._active_agents.append(name)
                self.engine._state.save_active(self.engine._active_agents)
            # 新增到 exec_agents
            if name not in self.exec_agents:
                self.exec_agents.append(name)
                self.total = len(self.exec_agents)
            runner = self._build_runner(name)
            if runner:
                self.mailbox.create(name)
                self._agent_tasks[asyncio.create_task(runner.run())] = name
                await self.engine._send(f"▶ {name} started (by state change)")
                if self.state_bus:
                    self.state_bus.set_agent_activity(name, "thinking")

        elif change_type == "agent_removed":
            # Leader 删掉了 agent block → 取消并移除
            raw_name = change["name"]
            name = self.engine._resolve_agent_name(raw_name) or raw_name
            self._cancel_task(name)
            self.engine.remove_agent(name)
            await self.engine._send(f"🚫 {name} removed (by state change)")

        elif change_type == "state_changed":
            raw_name = change["name"]
            name = self.engine._resolve_agent_name(raw_name) or raw_name
            new_state = change.get("new", "")
            if new_state == "paused":
                # Leader 暂停了 agent
                self._cancel_task(name)
                await self.engine._send(f"⏸ {name} paused (by state change)")

            elif new_state == "running":
                old_state = change.get("old", "")
                if old_state == "paused":
                    # 从 paused 恢复 → 重新启动
                    if not self._is_running(name):
                        runner = self._build_runner(name)
                        if runner:
                            self.mailbox.create(name)
                            self._agent_tasks[asyncio.create_task(runner.run())] = name
                            await self.engine._send(f"▶ {name} resumed (by state change)")

        elif change_type == "muted_changed":
            raw_name = change["name"]
            name = self.engine._resolve_agent_name(raw_name) or raw_name
            muted = change.get("muted", False)
            if muted:
                self.engine.mute_agent(name)
                await self.engine._send(f"🔇 {name} muted (by state change)")
            else:
                self.engine.unmute_agent(name)
                await self.engine._send(f"🔊 {name} unmuted (by state change)")

        elif change_type == "conversation_rewritten":
            # Leader 重写了 conversation — 同步到 engine._history
            if self.state_bus:
                data = self.state_bus.snapshot()
                conv = data.get("conversation", [])
                self.engine._history = [
                    {"sender": m.get("sender", "系统"), "content": str(m.get("content", ""))}
                    for m in conv
                ]
                await self.engine._send(f"🔄 History rewritten ({len(conv)} msgs)")

        elif change_type == "session_ended":
            # Leader 设置 session.status: done → 结束群聊
            await self.engine._send("🔚 Leader 结束群聊 (session.status: done)")
            for t in self._agent_tasks:
                if not t.done():
                    t.cancel()
            self.leader_end_event.set()

    # ── 内部工具方法 ────────────────────────────────────────

    def _is_running(self, name: str) -> bool:
        """检查 agent 是否正在运行。"""
        return any(n == name and not t.done() for t, n in self._agent_tasks.items())

    def _cancel_task(self, name: str) -> None:
        """取消指定 agent 的运行中 task。"""
        for t, n in self._agent_tasks.items():
            if n == name and not t.done():
                t.cancel()
                break


# ══════════════════════════════════════════════════════════════
# 公共 API
# ══════════════════════════════════════════════════════════════

async def broadcast_round(
    agents: list[str],
    engine: Any,
    mailbox: MailboxHub,
    global_timeout: float = 200.0,
) -> list[tuple[str, str | None]]:
    """⚠️ 公共 API — 签名不可改。

    执行一轮广播：setup → run → synthesize。
    返回 [(agent_name, content), ...] 按完成顺序。
    """
    if not agents:
        return []
    coord = BroadcastCoordinator(agents, engine, mailbox, global_timeout)
    coord.setup()
    await coord.run()
    await coord.synthesize()
    return coord.get_results()
