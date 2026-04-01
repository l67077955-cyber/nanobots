"""Leader-only tools for broadcast group chat.

Contains tools that only the leader agent can use:
- ``LeaderGate``: Enforces leader-gated speaking order
- ``ManageAgentTool``: Manage agents (disable/enable/set_tools)
- ``EndDiscussionTool``: End discussion phase
- ``TransferCreditsTool``: Transfer search credits between agents
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool


class LeaderGate:
    """Enforces leader-gated speaking order.

    Each non-leader agent may send at most 1 message between consecutive
    leader messages.  When the leader sends a message, all counters reset.
    """

    def __init__(self, leader_name: str) -> None:
        self._leader = leader_name
        # {agent_name: sends_since_leader_spoke}
        self._counts: dict[str, int] = {}

    def try_send(self, agent_name: str) -> bool:
        """Return True if the agent is allowed to send."""
        if agent_name == self._leader:
            return True
        return self._counts.get(agent_name, 0) < 1

    def record_send(self, agent_name: str) -> None:
        """Record that agent sent a message."""
        if agent_name == self._leader:
            # Leader spoke — reset everyone's counter
            for k in self._counts:
                self._counts[k] = 0
        else:
            self._counts[agent_name] = self._counts.get(agent_name, 0) + 1

    @property
    def leader(self) -> str:
        return self._leader


class ManageAgentTool(Tool):
    """Leader-only tool: manage agents during broadcast execution.

    Actions:
        disable  — Remove an agent from the current round
        enable   — Reactivate a previously disabled agent
        set_tools — Change an agent's tool permissions for this session
    """

    def __init__(
        self,
        *,
        exec_agents: list[str],
        agent_tasks: dict,  # asyncio.Task → name mapping
        engine: Any,
        mailbox: Any,
    ) -> None:
        self._exec_agents = exec_agents
        self._agent_tasks = agent_tasks  # {Task: name}
        self._engine = engine
        self._mailbox = mailbox
        self._disabled: set[str] = set()

    @property
    def name(self) -> str:
        return "manage_agent"

    @property
    def description(self) -> str:
        return (
            "管理 agent: disable(移除), enable(激活), set_tools(改工具权限)。"
            "仅当前轮有效。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["disable", "enable", "set_tools"],
                    "description": "操作类型",
                },
                "agent": {
                    "type": "string",
                    "description": "目标 agent 名字",
                },
                "tools": {
                    "type": "object",
                    "description": "工具权限 (仅 set_tools 时需要), 如 {\"web_search\": true, \"exec\": false}",
                },
            },
            "required": ["action", "agent"],
        }

    async def execute(
        self,
        action: str = "",
        agent: str = "",
        tools: dict | None = None,
        **kwargs: Any,
    ) -> str:
        if not action or not agent:
            return "Error: 必须指定 action 和 agent"

        if agent not in self._exec_agents:
            return f"Error: agent '{agent}' 不在当前轮中。可用: {', '.join(self._exec_agents)}"

        if action == "disable":
            if agent in self._disabled:
                return f"{agent} 已经被 disable 了"
            self._disabled.add(agent)
            # Cancel the agent's task
            for task_obj, task_name in self._agent_tasks.items():
                if task_name == agent and not task_obj.done():
                    task_obj.cancel()
                    break
            # Notify remaining active agents
            active = [a for a in self._exec_agents if a not in self._disabled]
            if active:
                self._mailbox.send("系统", active,
                    f"[系统通知] {agent} 已被 Leader 移除本轮讨论")
            await self._engine._send(f"⛔ Leader 已移除 {agent}")
            return f"✅ {agent} 已被 disable，其 task 已取消"

        elif action == "enable":
            if agent not in self._disabled:
                return f"{agent} 没有被 disable"
            self._disabled.discard(agent)
            # Notify
            active = [a for a in self._exec_agents if a not in self._disabled]
            if active:
                self._mailbox.send("系统", active,
                    f"[系统通知] {agent} 已被 Leader 重新激活")
            await self._engine._send(f"✅ Leader 已重新激活 {agent}")
            return f"✅ {agent} 已被 enable（注意：已取消的 task 不会自动重启）"

        elif action == "set_tools":
            if not tools or not isinstance(tools, dict):
                return "Error: set_tools 需要 tools 参数，如 {\"web_search\": true}"
            cfg = self._engine.registry.get(agent, {})
            current = cfg.get("tools", {})
            if isinstance(current, dict):
                current.update(tools)
            else:
                cfg["tools"] = dict(tools)
            # Notify
            active = [a for a in self._exec_agents if a not in self._disabled]
            changes = ", ".join(f"{k}={'开' if v else '关'}" for k, v in tools.items())
            if active:
                self._mailbox.send("系统", active,
                    f"[系统通知] Leader 已修改 {agent} 的工具权限: {changes}")
            await self._engine._send(f"🔧 Leader 修改 {agent} 权限: {changes}")
            return f"✅ {agent} 工具权限已更新: {changes}"

        return f"Error: 未知 action '{action}'"


class EndDiscussionTool(Tool):
    """Leader-only tool: end the discussion phase immediately.

    When called, sets an asyncio.Event that the broadcast loop watches.
    All agent tasks are cancelled and the leader enters the synthesis phase.
    """

    def __init__(self, *, end_event: Any, engine: Any) -> None:
        self._end_event = end_event  # asyncio.Event
        self._engine = engine

    @property
    def name(self) -> str:
        return "end_discussion"

    @property
    def description(self) -> str:
        return (
            "结束当前讨论，立即进入总结阶段。"
            "当你判断信息已经足够、讨论陷入循环、或 agent 表现不佳时使用。"
            "调用后所有 agent 会被停止，你将进入最终总结。"
            "参数: reason (可选，结束原因)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "结束讨论的原因（可选）",
                },
            },
            "required": [],
        }

    async def execute(self, reason: str = "", **kwargs: Any) -> str:
        reason_str = f"（原因: {reason}）" if reason else ""
        await self._engine._send(f"★ Leader 决定结束讨论{reason_str}")
        self._end_event.set()
        return f"✅ 讨论已结束{reason_str}，即将进入总结阶段"


class TransferCreditsTool(Tool):
    """Leader-only tool: transfer search credits between agents."""

    def __init__(self, *, search_pool: Any, engine: Any) -> None:
        self._pool = search_pool
        self._engine = engine

    @property
    def name(self) -> str:
        return "transfer_credits"

    @property
    def description(self) -> str:
        return (
            "划拨搜索额度：把一个 agent 的搜索额度转给另一个 agent。"
            "例如把没有搜索工具的 agent 的额度划给有搜索能力的 agent。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "from_agent": {
                    "type": "string",
                    "description": "从哪个 agent 划出额度",
                },
                "to_agent": {
                    "type": "string",
                    "description": "划给哪个 agent",
                },
                "amount": {
                    "type": "integer",
                    "description": "划拨数量（如不确定可填大数，系统会自动取可用最大值）",
                },
            },
            "required": ["from_agent", "to_agent", "amount"],
        }

    async def execute(self, from_agent: str = "", to_agent: str = "",
                      amount: int = 0, **kwargs: Any) -> str:
        if not from_agent or not to_agent:
            return "Error: 必须指定 from_agent 和 to_agent"
        success, msg = self._pool.transfer(from_agent, to_agent, amount)
        if success:
            await self._engine._send(f"🔄 {msg}")
            return f"{msg}\n当前额度: {self._pool.status()}"
        return f"Error: {msg}"
