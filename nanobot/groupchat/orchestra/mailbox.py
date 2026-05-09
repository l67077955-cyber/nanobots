"""Inter-agent mailbox system for group chat communication.

Provides an async message-passing hub so agents can send messages
to each other (or broadcast to all) and wait for replies.
"""

from __future__ import annotations

import asyncio
import random
import time as _time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class AgentMessage:
    """A single inter-agent message."""

    id: int = 0
    sender: str = ""
    content: str = ""
    targets: list[str] = field(default_factory=list)  # ["All"] for broadcast, or specific names
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
        if n > self._capacity:
            logger.warning(
                "ConversationPool: {} rejected — requested {} slots exceeds capacity {}",
                sender, n, self._capacity,
            )
            return False

        # If pool is full right now, log that we are waiting.
        if self._available < n:
            logger.debug(
                "ConversationPool: {} waiting for slots ({} needed, {} available)",
                sender, n, self._available,
            )

        acquired = 0
        try:
            for _ in range(n):
                await asyncio.wait_for(
                    self._sem.acquire(), timeout=self.ALLOCATE_TIMEOUT,
                )
                acquired += 1
                # Deduct from available as we acquire
                self._available -= 1
                
                # Re-check: user priority may have been set while waiting
                if not self._user_priority.is_set():
                    for _ in range(acquired):
                        self._sem.release()
                        self._available += 1
                    logger.info(
                        "ConversationPool: {} interrupted — user priority",
                        sender,
                    )
                    return False
        except asyncio.TimeoutError:
            # Release any partially acquired slots and restore available
            for _ in range(acquired):
                self._sem.release()
                self._available += 1
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

        # Acquire semaphore slots where available to keep _available in sync.
        acquired = 0
        for _ in range(n):
            if not self._sem.locked():
                try:
                    # Non-blocking: only acquire if slot is available right now
                    await asyncio.wait_for(self._sem.acquire(), timeout=0.01)
                    acquired += 1
                except (asyncio.TimeoutError, Exception):
                    break
            else:
                break
        
        # Always decrement _available by total n to keep it in sync with semaphore permits + overflow
        self._available -= n

        self._user_priority.set()
        logger.info(
            "ConversationPool: user allocated {} slots "
            "({} from sem, overflow={}, {} available), priority OFF",
            n, acquired, n - acquired, self._available,
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

        Releases the slot because the reply will allocate its own new slot(s).
        """
        pending = self._pending.get(agent_name, [])
        if to_sender in pending:
            pending.remove(to_sender)
            self._sem.release()
            self._available += 1
            logger.debug(
                "ConversationPool: {} replied to {} (released 1 slot, {} available)",
                agent_name, to_sender, self._available,
            )

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
        """Number of used slots (clamped to [0, capacity])."""
        return max(0, min(self._capacity, self._capacity - self._available))


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
        # Leader info + listener restrictions
        self._leader_name: str = ""
        self._listener_restrictions: dict[str, str] = {}
        # Track which agents are still active (not finished their tool_loop)
        self._active_agents: set[str] = set()
        # Track expected replies: {waiter: {active_agent_that_may_reply, ...}}
        # When agentB receives a message from agentA, agentA is added here so
        # agentA's auto-wait deadline is extended while agentB is still busy.
        self._expected_replies: dict[str, set[str]] = {}

        # ── Agent ranks (pawn < knight < bishop) ────────────
        # Controls interrupt hierarchy: higher rank can interrupt lower rank.
        # Same rank cannot interrupt each other.
        self._ranks: dict[str, int] = {}
        self._leader: str = ""  # Leader always has highest interrupt priority

        # ── Forced interrupt state ──────────────────────────
        # Per-agent asyncio.Event: set when a forced interrupt is triggered.
        # tool_loop checks this between operations and exits gracefully.
        self._interrupt_events: dict[str, asyncio.Event] = {}
        # Records WHO initiated each agent's latest interrupt (survives queue consumption).
        self._last_interrupt_sender: dict[str, str] = {}
        # Counts how many times each agent has been interrupted this round.
        # Hard limit: 3 interrupts per agent per round (raised from 1 so that
        # newer messages can re-trigger interrupts for freshness).
        self._interrupt_counts: dict[str, int] = {}
        # Agents currently inside tool_loop (busy — eligible for interruption).
        self._busy_agents: set[str] = set()
        # Auto-incrementing message ID for quote_message support
        self._next_msg_id: int = 0

    def set_leader_name(self, leader_name: str) -> None:
        """Set/update the leader name (may not be known at construction time)."""
        self._leader_name = leader_name

    def set_listener_restriction(self, agent: str, allowed_sender: str) -> None:
        """Restrict which sender's messages an agent can receive.

        Args:
            agent: The agent to restrict.
            allowed_sender: 'leader' = only leader, 'any' = clear restriction,
                           or a specific agent name.
        """
        if allowed_sender and allowed_sender.lower() == "any":
            self._listener_restrictions.pop(agent, None)
        else:
            resolved = allowed_sender
            if allowed_sender.lower() == "leader" and self._leader_name:
                resolved = self._leader_name
            self._listener_restrictions[agent] = resolved

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
        self._expected_replies.clear()
        self._global_start = _time.time()
        self._next_msg_id = 0
        # Reset interrupt state for the new round
        self._interrupt_counts.clear()
        self._busy_agents.clear()
        self._listener_restrictions.clear()
        for evt in self._interrupt_events.values():
            evt.clear()
        logger.debug("MailboxHub: round started, {} agents", len(self._queues))

    # ── Forced interrupt methods ─────────────────────────────────────────────

    def get_interrupt_event(self, agent_name: str) -> asyncio.Event:
        """Get (or create) the interrupt event for an agent.

        tool_loop polls this event between operations; when set, the loop
        exits gracefully with finish_reason='interrupted'.
        """
        if agent_name not in self._interrupt_events:
            self._interrupt_events[agent_name] = asyncio.Event()
        return self._interrupt_events[agent_name]

    def mark_busy(self, agent_name: str) -> None:
        """Mark an agent as busy inside tool_loop (interrupt-eligible)."""
        self._busy_agents.add(agent_name)
        logger.debug("MailboxHub: {} is now busy", agent_name)

    def mark_idle(self, agent_name: str) -> None:
        """Mark an agent as no longer inside tool_loop."""
        self._busy_agents.discard(agent_name)
        logger.debug("MailboxHub: {} is now idle", agent_name)

    def set_ranks(self, ranks: dict[str, str], leader: str = "") -> None:
        """Store agent ranks for interrupt permission checking.
        
        Args:
            ranks: Mapping of agent_name -> rank_string ("pawn", "knight", "bishop")
            leader: Leader agent name — always gets highest priority regardless of rank.
        """
        order = {"pawn": 0, "knight": 1, "bishop": 2}
        self._ranks.clear()
        self._leader = leader
        for name, r in ranks.items():
            # Leader always gets highest rank (bishop + 1)
            if name == leader:
                val = max(order.values()) + 1
            else:
                val = order.get(r, 0)
            self._ranks[name] = val
            logger.debug("MailboxHub: rank({}) = {} ({})", name, val, r)

    def _can_interrupt(self, sender: str, target: str) -> bool:
        """Check if sender has sufficient rank to interrupt target.
        
        Rules:
        - Leader CAN interrupt anyone (highest implicit priority)
        - Higher rank CAN interrupt lower rank
        - Equal rank CANNOT interrupt each other (they queue)
        - Unknown rank defaults to 0 (lowest)
        """
        s_rank = self._ranks.get(sender, 0)
        t_rank = self._ranks.get(target, 0)
        return s_rank > t_rank

    def _try_interrupt(self, target: str, sender: str) -> bool:
        """Attempt to interrupt a busy agent.

        Returns True if the interrupt was triggered, False if skipped
        (agent not busy, already interrupted once this round, or
        target == sender).
        """
        if target == sender:
            return False
        if target not in self._busy_agents:
            return False
        # Record who attempted the interrupt (before rank check, so blocked
        # attempts are also visible in the UI for debugging hierarchy bugs)
        self._last_interrupt_sender[target] = sender
        # Rank check: low-rank agents cannot interrupt higher-or-equal rank
        if not self._can_interrupt(sender, target):
            logger.debug(
                "MailboxHub: interrupt blocked — {} (rank {}) vs {} (rank {})",
                sender, self._ranks.get(sender, 0),
                target, self._ranks.get(target, 0),
            )
            return False
        if self._interrupt_counts.get(target, 0) >= 3:
            logger.debug(
                "MailboxHub: interrupt skipped for {} (already interrupted {} times this round)",
                target, self._interrupt_counts.get(target, 0),
            )
            return False
        # Record who caused this interrupt (persists beyond queue consumption)
        self._last_interrupt_sender[target] = sender
        # Trigger the interrupt
        evt = self.get_interrupt_event(target)
        evt.set()
        self._interrupt_counts[target] = self._interrupt_counts.get(target, 0) + 1
        logger.info(
            "MailboxHub: ⚡ interrupt triggered for {} by {} (count={}/1)",
            target, sender, self._interrupt_counts[target],
        )
        return True

    def interrupt_busy_agents(self, sender: str) -> int:
        """Set the interrupt event for every currently-busy agent.

        Used for high-priority signals (e.g. user messages) that should
        wake up all active tool_loops immediately.  Unlike ``_try_interrupt``,
        this does **not** increment ``_interrupt_counts`` so it doesn't
        consume the per-round agent—agent interrupt quota.

        Also resets each interrupted agent's interrupt count so that
        subsequent messages can still trigger fresh interrupts — this
        ensures the agent always responds to the *latest* high-priority
        message, not a stale one.

        Returns:
            Number of agents whose interrupt event was set.
        """
        count = 0
        for agent in list(self._busy_agents):
            if agent == sender:
                continue
            if sender != "用户" and not self._can_interrupt(sender, agent):
                continue
            self._last_interrupt_sender[agent] = sender
            evt = self.get_interrupt_event(agent)
            if not evt.is_set():
                evt.set()
                # Reset interrupt counter so the agent can be interrupted
                # again by newer messages after it re-enters tool_loop.
                self._interrupt_counts[agent] = 0
                count += 1
                logger.info(
                    "MailboxHub: ⚡ user interrupt set for busy agent {} (sender={}, counter reset)",
                    agent, sender,
                )
        if count:
            logger.info(
                "MailboxHub: user message interrupted {} busy agent(s)", count
            )
        return count

    # ── Message sending ──────────────────────────────────────────────────────

    def send(self, sender: str, targets: list[str], content: str) -> int:
        """Send a message to target agents. Returns number delivered.

        Args:
            sender: Name of the sending agent.
            targets: List of agent names, or ``["All"]`` for broadcast.
            content: Message content.

        Returns:
            Number of mailboxes the message was delivered to.
        """
        msg = AgentMessage(
            id=self._next_msg_id, sender=sender, content=content, targets=targets,
        )
        self._next_msg_id += 1
        self._history.append(msg)

        delivered = 0
        if "All" in targets or "all" in targets:
            # Broadcast to everyone except sender
            for name, q in self._queues.items():
                if name != sender:
                    # Check listener restriction before delivery
                    restriction = self._listener_restrictions.get(name)
                    if restriction and sender != restriction and sender != "系统":
                        continue
                    q.put_nowait(msg)
                    delivered += 1
                    # Trigger interrupt if recipient is busy (max 1/round)
                    self._try_interrupt(name, sender)
        else:
            for name in targets:
                q = self._queues.get(name)
                if q is not None and name != sender:
                    # Check listener restriction before delivery
                    restriction = self._listener_restrictions.get(name)
                    if restriction and sender != restriction and sender != "系统":
                        continue
                    q.put_nowait(msg)
                    delivered += 1
                    # Trigger interrupt if recipient is busy (max 1/round)
                    self._try_interrupt(name, sender)
                elif q is None:
                    logger.warning("MailboxHub: target '{}' has no mailbox", name)

        logger.info(
            "MailboxHub: {} → {} ({} delivered): {}",
            sender, targets, delivered, content[:100],
        )

        # Update expected-reply tracking:
        # Each recipient now has a message from `sender` in their queue —
        # sender may be waiting for their reply, so register sender as
        # expecting a reply from each recipient (while that recipient is active).
        # Also clear any prior expectation that sender was waiting on these recipients
        # (sender is clearly active now, not passively waiting).
        actual_targets: list[str]
        if "All" in targets or "all" in targets:
            actual_targets = [n for n in self._queues if n != sender]
        else:
            actual_targets = [t for t in targets if t != sender]

        for recipient in actual_targets:
            # sender expects recipient may reply back
            if sender not in self._expected_replies:
                self._expected_replies[sender] = set()
            self._expected_replies[sender].add(recipient)
            # recipient no longer needs to wait for sender (sender just sent)
            if recipient in self._expected_replies:
                self._expected_replies[recipient].discard(sender)

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
        timeout: float = 600.0,
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
            logger.info("MailboxHub: all {} agents waiting — nudging random agent",
                        len(self._active_agents))
            self._all_waiting.set()
            self._nudge_random_agent(reason="all-waiting")

        # Use the caller-provided timeout directly (no hard caps)
        effective_timeout = timeout

        # Poll interval: re-check expected-reply state at this cadence so we
        # can extend the deadline if a busy teammate is still working.
        _POLL_INTERVAL = 5.0
        deadline = _time.time() + effective_timeout

        _MAX_EXTENSIONS = 12  # max 12 × 5s = 60s extra beyond original timeout
        _extensions = 0
        try:
            while True:
                remaining = deadline - _time.time()
                if remaining <= 0:
                    # Before giving up: check whether any active agent is still
                    # expected to reply to us (i.e. they received our message and
                    # are busy processing — not yet in _waiting).  If so, extend
                    # the deadline to avoid premature idle-exit.
                    busy_repliers = self._get_busy_expected_repliers(agent_name)
                    if busy_repliers and _extensions < _MAX_EXTENSIONS:
                        _extensions += 1
                        extension = _POLL_INTERVAL
                        deadline = _time.time() + extension
                        logger.info(
                            "MailboxHub.wait: {} deadline extended +{}s "
                            "(busy repliers: {}, extension {}/{})",
                            agent_name, extension, busy_repliers,
                            _extensions, _MAX_EXTENSIONS,
                        )
                        continue
                    if busy_repliers:
                        logger.warning(
                            "MailboxHub.wait: {} giving up after {} extensions "
                            "— busy repliers {} never responded",
                            agent_name, _MAX_EXTENSIONS, busy_repliers,
                        )
                    else:
                        logger.info(
                            "MailboxHub.wait: timeout for {} ({}s)",
                            agent_name, effective_timeout,
                        )
                    return None
                try:
                    poll = min(remaining, _POLL_INTERVAL)
                    msg = await asyncio.wait_for(q.get(), timeout=poll)
                    # Filter by sender if requested — put back if wrong sender
                    # so other agents' messages are not silently discarded.
                    if from_agent and msg.sender != from_agent:
                        q.put_nowait(msg)
                        continue
                    logger.info(
                        "MailboxHub.wait: {} received from {}: {}",
                        agent_name, msg.sender, msg.content[:80],
                    )
                    return msg
                except asyncio.TimeoutError:
                    # Not a final timeout — loop back to check remaining/deadline
                    continue
        finally:
            self._waiting.discard(agent_name)
            # Clean up expected-reply entry for this agent
            self._expected_replies.pop(agent_name, None)

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

    def get_message(self, msg_id: int) -> AgentMessage | None:
        """Look up a message by its ID. Returns None if not found."""
        for msg in self._history:
            if msg.id == msg_id:
                return msg
        return None

    @property
    def agent_names(self) -> list[str]:
        """Names of agents with mailboxes."""
        return list(self._queues.keys())

    @property
    def all_waiting_event(self) -> asyncio.Event:
        """Event that fires when all active agents are simultaneously waiting."""
        return self._all_waiting

    def _get_busy_expected_repliers(self, agent_name: str) -> set[str]:
        """Return the set of agents that are expected to reply to agent_name
        and are still actively processing (not waiting, not done).

        These are agents that received a message from agent_name (so they may
        reply back) but have not yet entered the waiting state — meaning they
        are still running their tool_loop.
        """
        expected = self._expected_replies.get(agent_name, set())
        busy = set()
        for other in expected:
            if (
                other in self._active_agents   # still active (not done)
                and other not in self._waiting  # not currently idle-waiting
            ):
                busy.add(other)
        return busy

    def _nudge_random_agent(self, reason: str = "all-waiting") -> None:
        """Inject a nudge message to a random waiting agent.

        Called when all active agents are simultaneously waiting, which is
        a deadlock.  Picks one agent at random and injects a system message
        so it re-enters tool_loop and can either progress or end_discussion.
        """
        candidates = list(self._active_agents)
        if not candidates:
            return
        chosen = random.choice(candidates)
        nudge = AgentMessage(
            sender="系统",
            content=(
                "[全员空闲提醒] 所有队友都在等待中，没有新消息。\n"
                "请主动推进任务：总结当前进展、提出下一步行动，"
                "或（如果你是 Leader）调用 end_discussion 结束群聊。"
            ),
            targets=[chosen],
        )
        q = self._queues.get(chosen)
        if q is not None:
            q.put_nowait(nudge)
            logger.info(
                "MailboxHub: nudged {} to break {} deadlock", chosen, reason,
            )

    def mark_agent_done(self, agent_name: str) -> None:
        """Mark an agent as finished (no longer active)."""
        self._active_agents.discard(agent_name)
        self._waiting.discard(agent_name)
        # Remove this agent from all expected-reply sets so waiters don't
        # extend their deadline for a finished agent.
        for repliers in self._expected_replies.values():
            repliers.discard(agent_name)
        # Cleanup interrupt state
        self._busy_agents.discard(agent_name)
        self._interrupt_counts.pop(agent_name, None)
        if agent_name in self._interrupt_events:
            self._interrupt_events[agent_name].clear()
        # Re-check: if remaining active agents are all waiting
        if self._active_agents and self._waiting >= self._active_agents:
            self._all_waiting.set()
            self._nudge_random_agent(reason="agent-done")
