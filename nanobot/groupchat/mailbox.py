"""mailbox.py — Agent 间消息传递系统（MailboxHub）。

每个 agent 有一个异步队列（asyncio.Queue），用于接收消息。
Leader 通过 chatroom_send 工具或 control command 给其他 agent 发消息。
Agent 通过 wait 工具从自己的队列中取消息。

数据流：
    chatroom_send(to="Ares", message="搜索...")
      → MailboxHub.send("Kirk", ["Ares"], "搜索...")
        → 把 AgentMessage 放入 Ares 的 asyncio.Queue
          → Ares 调用 wait() 时从 Queue 取出

关键类：
    AgentMessage  — 一条消息（sender, content, targets, timestamp）
    MailboxHub    — 中心路由器，管理所有 agent 的 Queue

⚠️ agent 修改本文件时注意：
    1. send() 的 targets 参数：["All"] = 广播，["AgentName"] = 定向
    2. wait() 返回 None = 超时，返回 AgentMessage = 有消息
    3. _state_bus 同步是可选的，不影响核心功能
"""

from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class AgentMessage:
    """A single inter-agent message."""

    sender: str
    content: str
    targets: list[str]  # ["All"] for broadcast, or specific names
    timestamp: float = field(default_factory=_time.time)

    def __str__(self) -> str:
        to = ", ".join(self.targets)
        return f"[{self.sender} → {to}]: {self.content}"


class MailboxHub:
    """Central message router with per-agent async queues.

    Supports failure awareness: when an agent fails, all agents waiting
    for it receive an immediate notification instead of timing out.

    Usage::

        hub = MailboxHub()
        hub.create("Harper")
        hub.create("Benjamin")

        hub.send("Harper", ["Benjamin"], "What do you think?")

        msg = await hub.wait("Benjamin", timeout=30)
        # msg.sender == "Harper", msg.content == "What do you think?"
    """

    def __init__(
        self,
        on_message: Any | None = None,
        state_bus: Any | None = None,
    ) -> None:
        self._queues: dict[str, asyncio.Queue[AgentMessage]] = {}
        self._history: list[AgentMessage] = []
        self._global_start: float = 0.0
        self._global_timeout: float = 200.0
        self._on_message = on_message
        self._waiting: set[str] = set()
        self._all_waiting = asyncio.Event()
        self._active_agents: set[str] = set()
        self._failed_agents: dict[str, str] = {}
        self._state_bus = state_bus

    def create(self, agent_name: str) -> None:
        """Create a mailbox for an agent (idempotent)."""
        if agent_name not in self._queues:
            self._queues[agent_name] = asyncio.Queue()
            logger.debug("Mailbox created for {}", agent_name)

    def start_round(self, active_agents: list[str] | None = None) -> None:
        """Start a new round — reset all queues and history."""
        for q in self._queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._history.clear()
        self._waiting.clear()
        self._all_waiting.clear()
        self._failed_agents.clear()
        self._active_agents = set(active_agents) if active_agents else set(self._queues.keys())
        self._global_start = _time.time()
        logger.debug("MailboxHub: round started, {} agents", len(self._queues))

    def send(self, sender: str, targets: list[str], content: str) -> int:
        """Send a message to target agents. Returns number delivered."""
        msg = AgentMessage(sender=sender, content=content, targets=targets)
        self._history.append(msg)

        delivered = 0
        if "All" in targets or "all" in targets:
            for name, q in self._queues.items():
                if name != sender:
                    q.put_nowait(msg)
                    delivered += 1
        else:
            for name in targets:
                q = self._queues.get(name)
                if q is not None and name != sender:
                    q.put_nowait(msg)
                    delivered += 1
                elif q is None:
                    logger.warning("MailboxHub: target '{}' has no mailbox", name)

        logger.info(
            "MailboxHub: {} → {} ({} delivered): {}",
            sender, targets, delivered, content[:100],
        )

        # Notify persistence callback
        if self._on_message:
            try:
                self._on_message(sender, targets, content)
            except Exception:
                pass

        return delivered

    async def wait(
        self,
        agent_name: str,
        timeout: float = 120.0,
        from_agent: str = "",
    ) -> AgentMessage | None:
        """Wait for a message in the agent's mailbox.

        Terminates early if the waited-for agent has failed.
        """
        q = self._queues.get(agent_name)
        if q is None:
            logger.warning("MailboxHub.wait: no mailbox for {}", agent_name)
            return None

        # Early exit: if waiting for a specific agent that already failed
        if from_agent and from_agent in self._failed_agents:
            error = self._failed_agents[from_agent]
            return AgentMessage(
                sender="系统",
                content=f"[{from_agent} 已断线: {error}]",
                targets=[agent_name],
            )

        # Fast path: if there are already messages queued
        if not q.empty():
            try:
                msg = q.get_nowait()
                if not from_agent or msg.sender == from_agent:
                    return msg
                q.put_nowait(msg)
            except asyncio.QueueEmpty:
                pass

        # Register as waiting
        self._waiting.add(agent_name)
        if self._waiting >= self._active_agents and len(self._active_agents) > 0:
            logger.info("MailboxHub: all {} agents waiting — conversation done",
                        len(self._active_agents))
            self._all_waiting.set()

        # Enforce hard limits
        timeout = min(timeout, 120.0)
        elapsed = _time.time() - self._global_start if self._global_start else 0
        remaining_global = max(0, self._global_timeout - elapsed)
        effective_timeout = min(timeout, remaining_global)

        if effective_timeout <= 0:
            self._waiting.discard(agent_name)
            return None

        deadline = _time.time() + effective_timeout

        try:
            while True:
                remaining = deadline - _time.time()
                if remaining <= 0:
                    return None
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=remaining)
                    if from_agent and msg.sender != from_agent:
                        if msg.sender != "系统":
                            continue
                    # Mark inbox read in state.yaml
                    if self._state_bus:
                        try:
                            self._state_bus.mark_inbox_read(agent_name)
                        except Exception:
                            pass
                    return msg
                except asyncio.TimeoutError:
                    return None
        finally:
            self._waiting.discard(agent_name)

    def mark_agent_done(self, agent_name: str) -> None:
        """Mark an agent as finished (no longer active)."""
        self._active_agents.discard(agent_name)
        self._waiting.discard(agent_name)
        if self._active_agents and self._waiting >= self._active_agents:
            self._all_waiting.set()

    def mark_agent_failed(self, agent_name: str, error: str) -> None:
        """Mark an agent as failed and notify all waiting agents."""
        self._failed_agents[agent_name] = error
        self._active_agents.discard(agent_name)
        self._waiting.discard(agent_name)

        fail_msg = AgentMessage(
            sender="系统",
            content=f"[{agent_name} 已断线: {error}。请独立完成任务，不要等待该 agent。]",
            targets=list(self._active_agents),
        )
        for name, q in self._queues.items():
            if name != agent_name and name in self._active_agents:
                q.put_nowait(fail_msg)

        logger.info(
            "MailboxHub: {} marked as FAILED (error={}), notified {} agents",
            agent_name, error[:80], len(self._active_agents),
        )

        if self._active_agents and self._waiting >= self._active_agents:
            self._all_waiting.set()

    def clear(self) -> None:
        """Clear message queues but preserve history."""
        for q in self._queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    @property
    def history(self) -> list[AgentMessage]:
        return list(self._history)
