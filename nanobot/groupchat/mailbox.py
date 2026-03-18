"""Inter-agent mailbox system for group chat communication.

Provides an async message-passing hub so agents can send messages
to each other (or broadcast to all) and wait for replies.
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

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[AgentMessage]] = {}
        self._history: list[AgentMessage] = []
        self._global_start: float = 0.0
        self._global_timeout: float = 200.0  # hard global limit

    def create(self, agent_name: str) -> None:
        """Create a mailbox for an agent (idempotent)."""
        if agent_name not in self._queues:
            self._queues[agent_name] = asyncio.Queue()
            logger.debug("Mailbox created for {}", agent_name)

    def start_round(self) -> None:
        """Start a new round — reset all queues and history."""
        for q in self._queues.values():
            # Drain any leftover messages
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._history.clear()
        self._global_start = _time.time()
        logger.debug("MailboxHub: round started, {} agents", len(self._queues))

    def send(self, sender: str, targets: list[str], content: str) -> int:
        """Send a message to target agents. Returns number delivered.

        Args:
            sender: Name of the sending agent.
            targets: List of agent names, or ``["All"]`` for broadcast.
            content: Message content.

        Returns:
            Number of mailboxes the message was delivered to.
        """
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
        return delivered

    async def wait(
        self,
        agent_name: str,
        timeout: float = 120.0,
        from_agent: str = "",
    ) -> AgentMessage | None:
        """Wait for a message in the agent's mailbox.

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

        # Enforce hard limits
        timeout = min(timeout, 120.0)
        elapsed = _time.time() - self._global_start if self._global_start else 0
        remaining_global = max(0, self._global_timeout - elapsed)
        effective_timeout = min(timeout, remaining_global)

        if effective_timeout <= 0:
            logger.info("MailboxHub.wait: global timeout exceeded for {}", agent_name)
            return None

        deadline = _time.time() + effective_timeout

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
                    # Put back? No — just skip and keep waiting
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
