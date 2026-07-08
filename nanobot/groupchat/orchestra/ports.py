"""Architecture contract for the group-chat engine (Step 0.5+).

The three seams future feature code may depend on. Protocol only —
implementations live elsewhere (``agent_runner.AgentRunner`` for now; a
future ``ConversationContext`` and ``TurnStack`` will follow). New code must
depend on these Protocols, not on ``MailboxHub`` / ``GroupChatEngine``
internals. If a feature cannot be expressed through a port, extend the port
API rather than reaching past it.

See ``docs/groupchat-coupling-fix.md`` for the full plan and migration table.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentRunner(Protocol):
    """Per-agent runtime handle: owns the cancel signal + state queries.

    The single object future code touches to interrupt/cancel/inspect an
    agent at runtime. The key invariant: any blocking operation an agent
    performs (``wait``, an in-flight LLM call, future awaits) must race
    against ``interrupt_event`` — that is what makes cancellation reliable.
    """

    name: str

    @property
    def interrupt_event(self) -> asyncio.Event:
        """Cooperative cancel signal blocking operations race against."""
        ...

    @property
    def state(self) -> str:
        """Derived state: idle | busy | waiting | interrupted | done."""
        ...

    def force_interrupt(self, sender: str = "用户", reason: str = "⏹ 已中断") -> bool:
        """High-priority cooperative interrupt (user/leader); bypasses rank."""
        ...

    def request_interrupt(self, sender: str) -> bool:
        """Rank-checked cooperative interrupt by a peer agent."""
        ...

    def cancel(self, reason: str = "⏹ 已停止") -> None:
        """Hard stop: cooperative interrupt + task.cancel() (bounded unwind)."""
        ...


@runtime_checkable
class ConversationContext(Protocol):
    """Single owner of all mutable message state (Step 1 target).

    Once wired in, ``tool_loop`` receives a view from ``view_for`` rather than
    a raw mutable list, and all add/remove/prune/compress goes through here.
    """

    def add(self, role: str, content: str, **meta: Any) -> None:
        ...

    def forget(self, tool_call_ids: set[str]) -> None:
        ...

    def view_for(self, agent: str) -> list[dict[str, Any]]:
        ...

    async def compress(self) -> None:
        ...


@runtime_checkable
class TurnStack(Protocol):
    """Ordered queue of turn-frames (Step 2 target).

    Replaces ``speak_order`` + the per-agent ``while True`` cycle loop and its
    ~7 ``continue`` re-entry branches with a single ordered structure.
    """

    def push_turn(self, agent: str, trigger: Any) -> None:
        ...

    def interject(self, user_msg: str) -> None:
        ...

    async def next_frame(self) -> Any:
        ...
