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

from nanobot.groupchat.display.visibility import resolve_rank, rank_interrupt_level


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
    """Per-agent conversation pool with independent semaphores.

    Each agent has its own slot budget. Sending a message costs slots from
    the *sender's* pool (one per recipient). This prevents any single agent
    from monopolizing the conversation.

    - chatroom_send(to="All") with 3 recipients → costs 3 slots from sender
    - chatroom_send(to="Harper") → costs 1 slot from sender
    - When recipient calls wait() without replying → releases 1 slot back to sender
    - When sender's pool is empty → chatroom_send blocks until slots freed
    - User messages force-allocate from every recipient's pool (never blocked)
    """

    ALLOCATE_TIMEOUT = 15.0  # max seconds to wait for slots

    def register_agent(self, name: str, capacity: int) -> None:
        """Register a mid-round agent with an explicit slot budget."""
        if name in self._per_cap:
            return
        self._agents.append(name)
        self._per_cap[name] = capacity
        self._sems[name] = asyncio.Semaphore(capacity)
        self._available[name] = capacity
        self._pending[name] = []

    def __init__(
        self,
        capacity: int = 0,
        agents: list[str] | None = None,
        per_agent_capacity: dict[str, int] | None = None,
    ) -> None:
        self._agents = agents or []
        # Per-agent capacity: explicit dict wins, else uniform from capacity
        if per_agent_capacity:
            self._per_cap = dict(per_agent_capacity)
        else:
            uniform = capacity if capacity > 0 else max(len(self._agents) * 3, 1)
            self._per_cap = {a: uniform for a in self._agents}
        # Per-agent semaphores and available counters
        self._sems: dict[str, asyncio.Semaphore] = {
            a: asyncio.Semaphore(self._per_cap[a]) for a in self._agents
        }
        self._available: dict[str, int] = {
            a: self._per_cap[a] for a in self._agents
        }
        # Track pending replies: {recipient: [sender1, sender2, ...]}
        self._pending: dict[str, list[str]] = {a: [] for a in self._agents}
        # User priority: when cleared, agents are blocked from allocating
        self._user_priority = asyncio.Event()
        self._user_priority.set()

    async def allocate(self, sender: str, recipients: list[str]) -> bool:
        """Allocate slots from sender's pool. Blocks if sender's pool exhausted.

        Costs len(recipients) slots from sender's semaphore.
        Returns True if allocated, False if timeout or user priority active.
        """
        if not self._user_priority.is_set():
            logger.info(
                "ConversationPool: {} rejected — user priority active",
                sender,
            )
            return False

        n = len(recipients)
        cap = self._per_cap.get(sender, 0)
        if n > cap:
            logger.warning(
                "ConversationPool: {} rejected — requested {} slots exceeds capacity {}",
                sender, n, cap,
            )
            return False

        sem = self._sems.get(sender)
        avail = self._available.get(sender, 0)
        if sem is None:
            return False

        if avail < n:
            logger.debug(
                "ConversationPool: {} waiting for slots ({} needed, {} available)",
                sender, n, avail,
            )

        acquired = 0
        try:
            for _ in range(n):
                await asyncio.wait_for(
                    sem.acquire(), timeout=self.ALLOCATE_TIMEOUT,
                )
                acquired += 1
                self._available[sender] -= 1

                # Re-check: user priority may have been set while waiting
                if not self._user_priority.is_set():
                    for _ in range(acquired):
                        sem.release()
                        self._available[sender] += 1
                    logger.info(
                        "ConversationPool: {} interrupted — user priority",
                        sender,
                    )
                    return False
        except asyncio.TimeoutError:
            for _ in range(acquired):
                sem.release()
                self._available[sender] += 1
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
            "ConversationPool: {} → {} ({} slots from sender, {} available)",
            sender, recipients, n, self._available.get(sender, 0),
        )
        return True

    async def allocate_user(self, recipients: list[str]) -> None:
        """Force-allocate 1 slot from each recipient's pool for a user message.

        User messages consume from every recipient's pool so that agents
        can't bypass limits by only receiving user messages.
        _user_priority blocks agents during delivery.
        """
        self._user_priority.clear()
        n = len(recipients)
        logger.info("ConversationPool: user force-allocate {} slots (per-agent)", n)

        for r in recipients:
            if r in self._pending:
                self._pending[r].append("User")
            # Deduct 1 slot from each recipient's pool
            sem = self._sems.get(r)
            if sem:
                try:
                    await asyncio.wait_for(sem.acquire(), timeout=0.01)
                except (asyncio.TimeoutError, Exception):
                    pass  # overflow: still decrement _available
            self._available[r] = self._available.get(r, 0) - 1

        self._user_priority.set()
        logger.info(
            "ConversationPool: user allocated 1 slot each from {} recipients, priority OFF",
            n,
        )

    def release_unread(self, agent_name: str) -> int:
        """Release slots back to each pending sender's pool.

        Called when agent calls wait() without replying.
        Returns number of slots released.
        """
        pending = self._pending.get(agent_name, [])
        released = 0
        for sender in pending:
            sem = self._sems.get(sender)
            if sem:
                sem.release()
                self._available[sender] = self._available.get(sender, 0) + 1
                released += 1
            else:
                # Non-agent sender (e.g. "User"): release slot back to agent
                self._available[agent_name] = self._available.get(agent_name, 0) + 1
                released += 1
        self._pending[agent_name] = []
        if released > 0:
            logger.debug(
                "ConversationPool: {} released {} unread slots back to senders",
                agent_name, released,
            )
        return released

    def mark_replied(self, agent_name: str, to_sender: str) -> None:
        """Mark that agent replied to a message from to_sender.

        Releases 1 slot back to to_sender's pool.
        """
        pending = self._pending.get(agent_name, [])
        if to_sender in pending:
            pending.remove(to_sender)
            sem = self._sems.get(to_sender)
            if sem:
                sem.release()
                self._available[to_sender] = self._available.get(to_sender, 0) + 1
            else:
                # Non-agent sender (e.g. "User"): release slot back to agent
                self._available[agent_name] = self._available.get(agent_name, 0) + 1
            logger.debug(
                "ConversationPool: {} replied to {} (released 1 slot to sender)",
                agent_name, to_sender,
            )

    @property
    def available(self) -> int:
        """Total available slots across all agents (clamped to >= 0)."""
        return max(0, sum(self._available.values()))

    @property
    def capacity(self) -> int:
        """Total capacity across all agents."""
        return sum(self._per_cap.values())

    @property
    def used(self) -> int:
        """Total used slots across all agents."""
        return max(0, self.capacity - self.available)

    def agent_available(self, agent: str) -> int:
        """Available slots for a specific agent."""
        return max(0, self._available.get(agent, 0))

    def agent_capacity(self, agent: str) -> int:
        """Capacity for a specific agent."""
        return self._per_cap.get(agent, 0)

    def agent_used(self, agent: str) -> int:
        """Used slots for a specific agent."""
        return max(0, self.agent_capacity(agent) - self.agent_available(agent))

    def reset(self) -> None:
        """Reset all agent budgets to their per-round capacity.

        Called at the start of each broadcast round.
        """
        for a in self._agents:
            cap = self._per_cap.get(a, 0)
            self._sems[a] = asyncio.Semaphore(cap)
            self._available[a] = cap
            self._pending[a] = []
        logger.debug(
            "ConversationPool: reset — {} agents, total capacity {}",
            len(self._agents), self.capacity,
        )

    def status(self) -> str:
        """Per-agent pool breakdown: 'Kirk ▰▰▱▱▱ 2/5 · Harper ▰▱▱ 1/3'."""
        parts = []
        for a in self._agents:
            u = self.agent_used(a)
            c = self.agent_capacity(a)
            filled = "▰" * u
            empty = "▱" * (c - u)
            parts.append(f"{a} {filled}{empty} {u}/{c}")
        return " · ".join(parts)


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

        # ── Agent ranks (basic < standard < advanced < expert) ────────────
        # _base_ranks: configured tier for who-can-interrupt-whom comparisons.
        # _ranks: display/attribution; leader gets outbound boost only.
        self._base_ranks: dict[str, int] = {}
        self._ranks: dict[str, int] = {}
        self._leader: str = ""  # Leader as sender = high-priority interrupt

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
        # Discussion end state (for idempotency and final lock)
        self._discussion_ended: bool = False

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
        self._discussion_ended = False
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
            ranks: Mapping of agent_name -> rank_string ("basic", "standard", "advanced", "expert").
            leader: Leader agent name — gets high-priority **outbound** interrupts only
                (as sender). Inbound interrupts to leader use configured tier ranks so
                advanced teammates can still preempt a standard-tier leader.
        """
        self._ranks.clear()
        self._base_ranks: dict[str, int] = {}
        self._leader = leader
        for name, r in ranks.items():
            if isinstance(r, str):
                level = rank_interrupt_level(resolve_rank(r, agent=name))
            else:
                level = int(r)
            self._base_ranks[name] = level
            self._ranks[name] = level
        # Boosted rank for attribution / leader-as-sender priority display only
        if leader and leader in self._ranks:
            self._ranks[leader] = max(self._base_ranks.values()) + 1
        for name, val in self._base_ranks.items():
            r_str = ranks.get(name, "?")
            boost = f" → {self._ranks.get(name)}" if name == leader else ""
            logger.debug("MailboxHub: rank({}) = {}{} ({})", name, val, boost, r_str)

    def _is_high_priority(self, sender: str) -> bool:
        """Check if sender is a high-priority source (user or leader).

        High-priority senders bypass rank checks in interrupt_busy_agents,
        ensuring user/leader messages always wake up busy agents.
        """
        return sender == "用户" or sender == self._leader

    def _tier_rank(self, agent: str) -> int:
        """Configured tier rank for interrupt comparison (ignores leader outbound boost)."""
        return self._base_ranks.get(agent, self._ranks.get(agent, 0))

    def _can_interrupt(self, sender: str, target: str) -> bool:
        """Check if sender has sufficient rank to interrupt target.
        
        Rules:
        - Leader (or "用户") as **sender** CAN interrupt anyone
        - Higher **configured tier** CAN interrupt lower tier (advanced > standard)
        - Leader as **target** uses configured tier only — not immune to advanced peers
        - Equal tier CANNOT interrupt each other (they queue)
        """
        if self._is_high_priority(sender):
            return True
        s_rank = self._tier_rank(sender)
        t_rank = self._tier_rank(target)
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
        # Rank check: low-rank agents cannot interrupt higher-or-equal rank
        if not self._can_interrupt(sender, target):
            logger.info(
                "MailboxHub: interrupt blocked — {} (tier {}) cannot interrupt {} (tier {})",
                sender, self._tier_rank(sender),
                target, self._tier_rank(target),
            )
            return False
        if self._interrupt_counts.get(target, 0) >= 3:
            logger.debug(
                "MailboxHub: interrupt skipped for {} (already interrupted {} times this round)",
                target, self._interrupt_counts.get(target, 0),
            )
            return False
        # Record who caused this interrupt (only after rank + quota checks pass)
        # Only overwrite _last_interrupt_sender if the new sender has rank >= previous
        # interrupter's rank. (We already know sender can interrupt target.)
        # This ensures higher-authority interrupters keep the attribution credit.
        prev_sender = self._last_interrupt_sender.get(target)
        if prev_sender is None or self._ranks.get(sender, 0) >= self._ranks.get(prev_sender, 0):
            self._last_interrupt_sender[target] = sender
        # Trigger the interrupt (skip if already set — avoids double-counting)
        evt = self.get_interrupt_event(target)
        if evt.is_set():
            logger.debug(
                "MailboxHub: interrupt skipped for {} (event already set)", target,
            )
            return False
        evt.set()
        self._interrupt_counts[target] = self._interrupt_counts.get(target, 0) + 1
        logger.info(
            "MailboxHub: ⚡ interrupt triggered for {} by {} (count={}/3)",
            target, sender, self._interrupt_counts[target],
        )
        return True

    def interrupt_busy_agents(self, sender: str) -> int:
        """Set the interrupt event for every currently-busy agent.

        Used for high-priority signals (e.g. user/leader messages) that should
        wake up all active tool_loops immediately.  Unlike ``_try_interrupt``,
        this does **not** increment ``_interrupt_counts`` so it doesn't
        consume the per-round agent—agent interrupt quota.

        Only high-priority senders (user or leader) bypass rank checks;
        other senders must still have higher rank than the target agent.

        Returns:
            Number of agents whose interrupt event was set.
        """
        count = 0
        for agent in list(self._busy_agents):
            if agent == sender:
                continue
            if not self._is_high_priority(sender) and not self._can_interrupt(sender, agent):
                continue
            # Only overwrite _last_interrupt_sender if new sender has >= rank
            prev_sender = self._last_interrupt_sender.get(agent)
            if prev_sender is None or self._ranks.get(sender, 0) >= self._ranks.get(prev_sender, 0):
                self._last_interrupt_sender[agent] = sender
            evt = self.get_interrupt_event(agent)
            if not evt.is_set():
                evt.set()
                count += 1
                logger.info(
                    "MailboxHub: ⚡ high-priority interrupt set for busy agent {} (sender={})",
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
                    # NOTE: interrupt is now handled by _trigger_realtime_interrupts
                    # in broadcast_view.py, so we no longer call _try_interrupt here
                    # to avoid double-interrupting the same target.
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
                    # NOTE: interrupt is now handled by _trigger_realtime_interrupts
                    # in broadcast_view.py, so we no longer call _try_interrupt here
                    # to avoid double-interrupting the same target.
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

        # ── Proactive nudge: if waiting for a specific busy agent, set their
        # interrupt event so they know someone is waiting for a reply.  This
        # avoids the "oblivious busy agent" problem where an agent in tool_loop
        # continues working without realising a teammate's wait() is blocked
        # on them.  The interrupt causes the busy agent's tool_loop to break
        # out at the next asyncio checkpoint, giving it a chance to reply.
        if from_agent and from_agent in self._busy_agents:
            # Rank check: only nudge if caller has higher rank than target
            if self._can_interrupt(agent_name, from_agent):
                evt = self.get_interrupt_event(from_agent)
                if not evt.is_set():
                    evt.set()
                    logger.info(
                        "MailboxHub.wait: {} nudging busy agent {} (interrupt set, "
                        "from_agent is in tool_loop)",
                        agent_name, from_agent,
                    )
            else:
                logger.debug(
                    "MailboxHub.wait: nudge blocked — {} (rank {}) cannot nudge {} (rank {})",
                    agent_name, self._ranks.get(agent_name, 0),
                    from_agent, self._ranks.get(from_agent, 0),
                )

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

        NOTE: Only agents that are *busy* (inside tool_loop, tracked by
        _busy_agents) qualify.  An agent that is active but idle (between
        tool_loop runs, e.g. in mailbox.wait itself) is NOT considered a
        busy replier — they had their chance to reply and chose to wait
        instead, so extending the caller's deadline won't help.
        """
        expected = self._expected_replies.get(agent_name, set())
        busy = set()
        for other in expected:
            if (
                other in self._active_agents   # still active (not done)
                and other not in self._waiting  # not currently idle-waiting
                and other in self._busy_agents   # actively inside tool_loop
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

    def is_discussion_ended(self) -> bool:
        """Whether end_discussion has been successfully called (idempotency / final lock)."""
        return self._discussion_ended

    def mark_discussion_ended(self) -> None:
        """Mark the discussion as ended (prevents re-entrancy and further busy work)."""
        self._discussion_ended = True
        # Also ensure all_waiting to unblock any lingering wait() calls
        self._all_waiting.set()
        # Force-clear busy/interrupt state so any mid-turn agents exit their tool_loops promptly
        # (important for interrupt + end races and the old "waiting guard" brittleness).
        for name in list(self._busy_agents):
            self.mark_idle(name)
        for evt in self._interrupt_events.values():
            evt.set()  # wake any polling loops
        self._interrupt_counts.clear()
