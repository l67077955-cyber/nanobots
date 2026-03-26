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


class ConversationPool:
    """OS-style resource pool for conversation chain management.

    Controls message volume by treating conversation chains as a limited
    resource. Each message consumes slots proportional to its recipient
    count, naturally penalizing All-broadcasts.

    - Pool capacity = agent_count × 3 (configurable)
    - chatroom_send(to="All") with 3 other agents → costs 3 slots
    - chatroom_send(to="Harper") → costs 1 slot
    - When recipient calls wait() without replying → releases 1 slot
    - When pool is empty → chatroom_send blocks until slots freed
    """

    ALLOCATE_TIMEOUT = 15.0  # max seconds to wait for slots

    def __init__(self, capacity: int, agents: list[str] | None = None) -> None:
        self._capacity = capacity
        self._available = capacity
        self._sem = asyncio.Semaphore(capacity)
        self._agents = agents or []
        # Track pending replies: {recipient: [sender1, sender2, ...]}
        # When recipient wait()s without replying, these are released
        self._pending: dict[str, list[str]] = {a: [] for a in self._agents}
        # User priority: when cleared, agents are blocked from allocating
        self._user_priority = asyncio.Event()
        self._user_priority.set()  # default: no user priority, agents can proceed

    async def allocate(self, sender: str, recipients: list[str]) -> bool:
        """Allocate slots for a message. Blocks if pool exhausted.

        Costs len(recipients) slots. One slot per recipient.
        Returns True if allocated, False if timeout (pool full too long).

        When user priority is active, agents are immediately rejected.
        """
        # Agent: if user has priority, fail immediately
        if not self._user_priority.is_set():
            logger.info(
                "ConversationPool: {} rejected — user priority active",
                sender,
            )
            return False

        n = len(recipients)

        # Check real available slots (user force-alloc may have drained them)
        if self._available < n:
            logger.warning(
                "ConversationPool: {} rejected — not enough slots "
                "({} needed, {} available)",
                sender, n, self._available,
            )
            return False

        acquired = 0
        try:
            for _ in range(n):
                await asyncio.wait_for(
                    self._sem.acquire(), timeout=self.ALLOCATE_TIMEOUT,
                )
                acquired += 1
                # Re-check: user priority may have been set while waiting
                if not self._user_priority.is_set():
                    for _ in range(acquired):
                        self._sem.release()
                    logger.info(
                        "ConversationPool: {} interrupted — user priority",
                        sender,
                    )
                    return False
        except asyncio.TimeoutError:
            # Release any partially acquired slots
            for _ in range(acquired):
                self._sem.release()
            logger.warning(
                "ConversationPool: {} failed to allocate {} slots "
                "(acquired {}, pool full)",
                sender, n, acquired,
            )
            return False

        # Record pending replies for each recipient
        for r in recipients:
            if r in self._pending:
                self._pending[r].append(sender)

        self._available -= n

        logger.debug(
            "ConversationPool: {} → {} ({} slots used, {} available)",
            sender, recipients, n, self._available,
        )
        return True

    async def allocate_user(self, recipients: list[str]) -> None:
        """Force-allocate slots for a user message — never blocked.

        User messages go directly into the pool, even if it exceeds
        capacity. Agents still follow normal wait/timeout/drop logic.
        _user_priority blocks agents during delivery.
        """
        self._user_priority.clear()
        n = len(recipients)
        logger.info("ConversationPool: user force-allocate {} slots", n)

        for r in recipients:
            if r in self._pending:
                self._pending[r].append("User")

        self._available -= n  # can go negative (over capacity)

        self._user_priority.set()
        logger.info(
            "ConversationPool: user allocated {} slots ({} available), priority OFF",
            n, self._available,
        )

    def release_unread(self, agent_name: str) -> int:
        """Release slots for messages this agent received but didn't reply to.

        Called when agent calls wait() or finishes a cycle.
        Returns number of slots released.
        """
        pending = self._pending.get(agent_name, [])
        released = len(pending)
        for _ in range(released):
            self._sem.release()
        self._pending[agent_name] = []
        self._available += released
        if released > 0:
            logger.debug(
                "ConversationPool: {} released {} unread slots ({} available)",
                agent_name, released, self._available,
            )
        return released

    def mark_replied(self, agent_name: str, to_sender: str) -> None:
        """Mark that agent replied to a message from to_sender.

        The slot stays consumed (active conversation continues).
        Only removes one pending entry (one reply per received message).
        """
        pending = self._pending.get(agent_name, [])
        if to_sender in pending:
            pending.remove(to_sender)

    @property
    def available(self) -> int:
        """Number of available slots (clamped to >= 0)."""
        return max(0, self._available)

    @property
    def capacity(self) -> int:
        """Total pool capacity."""
        return self._capacity

    @property
    def used(self) -> int:
        """Number of used slots (clamped to capacity)."""
        return min(self._capacity, self._capacity - self._available)


# Keep SpeakQueue as alias for backward compat (referenced in imports)
SpeakQueue = ConversationPool


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
        self._active_agents = set(active_agents) if active_agents else set(self._queues.keys())
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

        # Fast path: if there are already messages queued, return immediately
        # WITHOUT marking as waiting.  This prevents the all_waiting_event
        # from firing prematurely when a teammate's message is already in the
        # queue (e.g. auto-shared output that arrived before we entered wait).
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

    def mark_agent_done(self, agent_name: str) -> None:
        """Mark an agent as finished (no longer active)."""
        self._active_agents.discard(agent_name)
        self._waiting.discard(agent_name)
        # Re-check: if remaining active agents are all waiting
        if self._active_agents and self._waiting >= self._active_agents:
            self._all_waiting.set()
