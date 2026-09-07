"""Channel-agnostic status panel abstraction.

Defines a ``StatusPanel`` protocol so non-Telegram channels can also render a
live agent-status dashboard. The existing ``AgentStatusTracker`` (in
``nanobot.groupchat.orchestra.broadcast``) already satisfies this protocol —
it IS the Telegram implementation. ``NullStatusPanel`` is the no-op fallback
for channels without in-place message editing (CLI, headless).

See plan.md Phase 3.4.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StatusPanel(Protocol):
    """Live status dashboard for agents during a broadcast round.

    Implementations edit a single message in place (Telegram) or render to
    whatever surface the channel supports. The protocol is structural —
    ``AgentStatusTracker`` satisfies it without inheritance.
    """

    async def create_panel(self) -> None:
        """Send the initial panel message (or otherwise initialise the view)."""
        ...

    async def set_state(
        self,
        agent: str,
        state: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        """Update one agent's displayed state and refresh (throttled)."""
        ...

    async def finalize(self) -> None:
        """Force a final refresh, ignoring the throttle interval."""
        ...

    def add_agent(self, name: str) -> None:
        """Register an agent that joined the round mid-flight."""
        ...

    async def update_from_tool_start(self, agent: str, tool_name: str, args: dict) -> None:
        """Derive a state from a tool-call's arguments (e.g. web_search → searching)."""
        ...

    async def update_from_tool_result(self, agent: str, tool_name: str, result: str) -> None:
        """Derive a state from a tool's result (e.g. BLOCKED → blocked)."""
        ...


class NullStatusPanel:
    """No-op status panel for channels without in-place editing.

    Every method is a no-op, so orchestration code can call the same surface
    regardless of whether the channel can actually render a panel. This is
    the graceful-degradation behaviour ``AgentStatusTracker`` already has
    when ``edit_fn`` is None, factored into its own type.
    """

    def __init__(self, agents: list[str] | None = None, leader: str | None = None) -> None:
        self._agents = list(agents or [])
        self._leader = leader

    async def create_panel(self) -> None:
        pass

    async def set_state(
        self,
        agent: str,
        state: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        pass

    async def finalize(self) -> None:
        pass

    def add_agent(self, name: str) -> None:
        if name not in self._agents:
            self._agents.append(name)

    async def update_from_tool_start(self, agent: str, tool_name: str, args: dict) -> None:
        pass

    async def update_from_tool_result(self, agent: str, tool_name: str, result: str) -> None:
        pass


class DiscordStatusPanel:
    """Placeholder status panel for Discord.

    Discord supports embed editing, so a real implementation would track an
    embed ID and update fields. For now this is a stub that logs at debug
    level — wired so orchestration doesn't crash on a Discord session.
    """

    def __init__(self, agents: list[str] | None = None, leader: str | None = None) -> None:
        self._agents = list(agents or [])
        self._leader = leader

    async def create_panel(self) -> None:
        from loguru import logger

        logger.debug("DiscordStatusPanel.create_panel (stub): {} agents", len(self._agents))

    async def set_state(
        self,
        agent: str,
        state: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        pass

    async def finalize(self) -> None:
        pass

    def add_agent(self, name: str) -> None:
        if name not in self._agents:
            self._agents.append(name)

    async def update_from_tool_start(self, agent: str, tool_name: str, args: dict) -> None:
        pass

    async def update_from_tool_result(self, agent: str, tool_name: str, result: str) -> None:
        pass


class MatrixStatusPanel:
    """Placeholder status panel for Matrix.

    Matrix supports room state events and message edits. A real implementation
    would post a state event and update it. Stub for now.
    """

    def __init__(self, agents: list[str] | None = None, leader: str | None = None) -> None:
        self._agents = list(agents or [])
        self._leader = leader

    async def create_panel(self) -> None:
        from loguru import logger

        logger.debug("MatrixStatusPanel.create_panel (stub): {} agents", len(self._agents))

    async def set_state(
        self,
        agent: str,
        state: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        pass

    async def finalize(self) -> None:
        pass

    def add_agent(self, name: str) -> None:
        if name not in self._agents:
            self._agents.append(name)

    async def update_from_tool_start(self, agent: str, tool_name: str, args: dict) -> None:
        pass

    async def update_from_tool_result(self, agent: str, tool_name: str, result: str) -> None:
        pass


def make_status_panel(
    channel: str,
    agents: list[str],
    leader: str | None,
    edit_fn: Any | None = None,
    send_and_get_id_fn: Any | None = None,
) -> StatusPanel:
    """Factory: pick a StatusPanel implementation for a channel.

    For Telegram (or any channel that provides edit_fn), return the real
    AgentStatusTracker. For channels without in-place editing, return the
    appropriate stub or NullStatusPanel.

    Args:
        channel: Channel name (``telegram``, ``discord``, ``matrix``, …).
        agents: Agent names in speak order.
        leader: The leader agent name, if any.
        edit_fn: Optional async (msg_id, text) -> None for editing.
        send_and_get_id_fn: Optional async (text) -> msg_id for the initial send.
    """
    # Telegram (and any channel with edit capability): real tracker.
    if edit_fn is not None and send_and_get_id_fn is not None:
        from nanobot.groupchat.orchestra.broadcast import AgentStatusTracker

        return AgentStatusTracker(agents, leader, edit_fn, send_and_get_id_fn)  # type: ignore[return-value]

    # Channel-specific stubs.
    if channel == "discord":
        return DiscordStatusPanel(agents, leader)
    if channel == "matrix":
        return MatrixStatusPanel(agents, leader)

    # Default: no-op.
    return NullStatusPanel(agents, leader)
