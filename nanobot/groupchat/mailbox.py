"""Inter-agent mailbox system for group chat communication.

Provides an async message-passing hub so agents can send messages
to each other (or broadcast to all) and wait for replies.

Also provides ``MessageThrottle`` — a simple per-agent message counter
that replaces the complex semaphore-based ConversationPool.
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


class MessageThrottle:
    """Simple per-agent message counter replacing ConversationPool.

    Tracks how many messages each agent has sent and enforces a hard limit.
    No semaphores, no pending tracking, no user priority — just counting.
    """

    def __init__(self, max_per_agent: int = 10, max_total: int = 30) -> None:
        self._max_per_agent = max_per_agent
        self._max_total = max_total
        self._counts: dict[str, int] = {}
        self._total = 0

    def can_send(self, agent: str) -> bool:
        """Check if agent is allowed to send."""
        if self._total >= self._max_total:
            return False
        return self._counts.get(agent, 0) < self._max_per_agent

    def record_send(self, agent: str, recipient_count: int = 1) -> None:
        """Record a sent message."""
        self._counts[agent] = self._counts.get(agent, 0) + 1
        self._total += 1

    def reset(self) -> None:
        """Reset all counters."""
        self._counts.clear()
        self._total = 0

    @property
    def used(self) -> int:
        """Total messages sent."""
        return self._total

    @property
    def capacity(self) -> int:
        """Maximum total messages."""
        return self._max_total

    @property
    def available(self) -> int:
        return max(0, self._max_total - self._total)


# ── Legacy aliases for backward compatibility ──────────────────
# Some imports still reference these names; they delegate to MessageThrottle.

class ConversationPool:
    """Legacy wrapper — delegates to MessageThrottle for backward compat.

    The old semaphore-based pool is replaced with simple counting.
    Maintains the same public API surface used by broadcast.py and chatroom_tools.
    """

    ALLOCATE_TIMEOUT = 15.0

    def __init__(self, capacity: int, agents: list[str] | None = None) -> None:
        self._capacity = capacity
        self._agents = agents or []
        self._throttle = MessageThrottle(
            max_per_agent=max(capacity // max(len(self._agents), 1), 5),
            max_total=capacity,
        )

    async def allocate(self, sender: str, recipients: list[str]) -> bool:
        """Check if sender can send. No blocking — immediate yes/no."""
        if not self._throttle.can_send(sender):
            logger.warning(
                "ConversationPool: {} rejected — throttle limit reached",
                sender,
            )
            return False
        self._throttle.record_send(sender, len(recipients))
        return True

    async def allocate_user(self, recipients: list[str]) -> None:
        """User messages always go through — just record."""
        self._throttle._total += 1

    def release_unread(self, agent_name: str) -> int:
        """No-op in simplified model. Returns 0."""
        return 0

    def mark_replied(self, agent_name: str, to_sender: str) -> None:
        """No-op in simplified model."""
        pass

    @property
    def available(self) -> int:
        return self._throttle.available

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def used(self) -> int:
        return self._throttle.used


# Keep SpeakQueue as alias for backward compat (referenced in imports)
SpeakQueue = ConversationPool


class MailboxHub:
    """Central message router with per-agent async queues.

    Supports failure awareness: when an agent fails, all agents waiting
    for it receive an immediate notification instead of timing out.

    Usage::

        hub = MailboxHub()
        hub.create("Harper")
        hub.create("Benjamin")

        # Agent sends a message
        hub.send("Harper", ["Benjamin"], "What do you think?")

        # Target agent waits for it
        msg = await hub.wait("Benjamin", timeout=30)
        # msg.sender == "Harper", msg.content == "What do you think?"
    """

    def __init__(
        self,
        on_message: Any | None = None,
    ) -> None:
        self._queues: dict[str, asyncio.Queue[AgentMessage]] = {}
        self._history: list[AgentMessage] = []
        self._global_start: float = 0.0
        self._global_timeout: float = 200.0  # hard global limit
        # Optional callback: called with (sender, targets, content) on every send()
        self._on_message = on_message
        # Track which agents are currently waiting
        self._waiting: set[str] = set()
        self._all_waiting = asyncio.Event()
        # Track which agents are still active (not finished their tool_loop)
        self._active_agents: set[str] = set()
        # Track failed agents
        self._failed_agents: dict[str, str] = {}  # name → error message

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
            # Broadcast to everyone except sender
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

        Args:
            agent_name: The waiting agent's name.
            timeout: Max seconds to wait (hard cap: 120s per call).
            from_agent: If set, only return messages from this sender.

        Returns:
            The message, or ``None`` on timeout.
        """
        q = self._queues.get(agent_name)
        if q is None:
            logger.warning("MailboxHub.wait: no mailbox for {}", agent_name)
            return None

        # Early exit: if waiting for a specific agent that already failed
        if from_agent and from_agent in self._failed_agents:
            error = self._failed_agents[from_agent]
            logger.info(
                "MailboxHub.wait: {} — target {} already failed: {}",
                agent_name, from_agent, error,
            )
            return AgentMessage(
                sender="系统",
                content=f"[{from_agent} 已断线: {error}]",
                targets=[agent_name],
            )

        # Fast path: if there are already messages queued, return immediately
        # WITHOUT marking as waiting.
        if not q.empty():
            try:
                msg = q.get_nowait()
                if not from_agent or msg.sender == from_agent:
                    logger.info(
                        "MailboxHub.wait: {} fast-path from {}: {}",
                        agent_name, msg.sender, msg.content[:80],
                    )
                    return msg
                # Wrong sender — put back and fall through to blocking wait
                q.put_nowait(msg)
            except asyncio.QueueEmpty:
                pass

        # Register as waiting (only if no message was immediately available)
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
            logger.info("MailboxHub.wait: global timeout exceeded for {}", agent_name)
            return None

        deadline = _time.time() + effective_timeout

        try:
            while True:
                remaining = deadline - _time.time()
                if remaining <= 0:
                    logger.info(
                        "MailboxHub.wait: timeout for {} ({}s)",
                        agent_name, effective_timeout,
                    )
                    return None
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=remaining)
                    # Filter by sender if requested
                    if from_agent and msg.sender != from_agent:
                        # Accept system messages (failure notifications) regardless of filter
                        if msg.sender != "系统":
                            continue
                    logger.info(
                        "MailboxHub.wait: {} received from {}: {}",
                        agent_name, msg.sender, msg.content[:80],
                    )
                    return msg
                except asyncio.TimeoutError:
                    logger.info(
                        "MailboxHub.wait: timeout for {} ({}s)",
                        agent_name, effective_timeout,
                    )
                    return None
        finally:
            self._waiting.discard(agent_name)

    def mark_agent_done(self, agent_name: str) -> None:
        """Mark an agent as finished (no longer active)."""
        self._active_agents.discard(agent_name)
        self._waiting.discard(agent_name)
        # Re-check: if remaining active agents are all waiting
        if self._active_agents and self._waiting >= self._active_agents:
            self._all_waiting.set()

    def mark_agent_failed(self, agent_name: str, error: str) -> None:
        """Mark an agent as failed and notify all waiting agents.

        This is the key improvement: agents waiting for the failed agent
        receive an immediate system notification instead of timing out.
        """
        self._failed_agents[agent_name] = error
        self._active_agents.discard(agent_name)
        self._waiting.discard(agent_name)

        # Inject failure notification into ALL other agents' queues
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

        # Re-check all-waiting (fewer active agents now)
        if self._active_agents and self._waiting >= self._active_agents:
            self._all_waiting.set()

    def clear(self) -> None:
        """Clear message queues but preserve history for later reading."""
        for q in self._queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def destroy(self) -> None:
        """Remove all mailboxes."""
        self._queues.clear()
        self._history.clear()

    @property
    def history(self) -> list[AgentMessage]:
        """All messages sent this round."""
        return list(self._history)

    @property
    def agent_names(self) -> list[str]:
        """Names of agents with mailboxes."""
        return list(self._queues.keys())

    @property
    def all_waiting_event(self) -> asyncio.Event:
        """Event that fires when all active agents are simultaneously waiting."""
        return self._all_waiting

    @property
    def active_agent_count(self) -> int:
        """Number of actively running agents."""
        return len(self._active_agents)
