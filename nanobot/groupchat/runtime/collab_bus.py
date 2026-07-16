"""CollabBus — narrow agent-to-agent message delivery surface.

Durable conversation context is ``nanobot.core.history.History`` only.
This port is the *ephemeral* per-round delivery bus (queues + round_log).

Implementations: ``MailboxHub`` (still co-hosts interrupt/busy hooks until Phase 2).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nanobot.groupchat.runtime.mailbox import AgentMessage


@runtime_checkable
class CollabBus(Protocol):
    """Agent collaboration delivery API (send / wait / round index).

    Not a second transcript store. ``round_log`` is round-scoped delivery
    metadata for list_messages / quote_message only.
    """

    def create(self, agent_name: str) -> None:
        """Ensure a delivery queue exists for ``agent_name``."""
        ...

    def start_round(self, active_agents: list[str] | None = None) -> None:
        """Reset per-round delivery state (queues drain / round_log clear)."""
        ...

    def send(self, sender: str, targets: list[str], content: str) -> int:
        """Fan-out message to targets. Returns number of queues delivered to."""
        ...

    async def wait(
        self,
        agent_name: str,
        timeout: float = 600.0,
        from_agent: str = "",
    ) -> AgentMessage | None:
        """Block until a message arrives, interrupt, or timeout."""
        ...

    @property
    def round_log(self) -> list[AgentMessage]:
        """Copy of this round's delivery log (not History)."""
        ...

    def get_message(self, msg_id: int) -> AgentMessage | None:
        """Lookup by id in ``round_log``."""
        ...

    @property
    def agent_names(self) -> list[str]:
        """Names that currently have a mailbox queue."""
        ...

    def clear(self) -> None:
        """Clear queues and round_log (end of round cleanup)."""
        ...


def deliver(bus: CollabBus, sender: str, targets: list[str], content: str) -> int:
    """Single entry for implicit system/agent deliveries (non-tool path).

    Call sites in agent_cycle / broadcast must use this instead of reaching
    into queue internals.
    """
    return bus.send(sender, targets, content)


__all__ = ["CollabBus", "AgentMessage", "deliver"]
