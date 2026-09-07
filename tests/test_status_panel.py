"""Tests for the StatusPanel abstraction. See plan.md Phase 3.4."""

import asyncio

import pytest

from nanobot.groupchat.display.status_panel import (
    DiscordStatusPanel,
    MatrixStatusPanel,
    NullStatusPanel,
    StatusPanel,
    make_status_panel,
)


def test_null_status_panel_is_noop():
    """NullStatusPanel methods are no-ops that don't raise."""
    panel = NullStatusPanel(agents=["A", "B"], leader="A")
    asyncio.run(panel.create_panel())
    asyncio.run(panel.set_state("A", "thinking", detail="x"))
    asyncio.run(panel.finalize())
    asyncio.run(panel.update_from_tool_start("A", "web_search", {"query": "q"}))
    asyncio.run(panel.update_from_tool_result("A", "web_search", "done"))


def test_null_status_panel_add_agent():
    panel = NullStatusPanel(agents=["A"])
    panel.add_agent("B")
    panel.add_agent("A")  # dedup
    assert panel._agents == ["A", "B"]


def test_discord_status_panel_stub_runs():
    panel = DiscordStatusPanel(agents=["A"], leader="A")
    asyncio.run(panel.create_panel())
    asyncio.run(panel.set_state("A", "searching"))
    asyncio.run(panel.finalize())


def test_matrix_status_panel_stub_runs():
    panel = MatrixStatusPanel(agents=["A"], leader="A")
    asyncio.run(panel.create_panel())
    asyncio.run(panel.set_state("A", "searching"))
    asyncio.run(panel.finalize())


def test_agent_status_tracker_satisfies_protocol():
    """The existing AgentStatusTracker is structurally compatible with StatusPanel."""
    from nanobot.groupchat.orchestra.broadcast import AgentStatusTracker

    tracker = AgentStatusTracker(["A"], "A", edit_fn=None, send_and_get_id_fn=None)
    # Structural check: the protocol methods exist.
    for method in ("create_panel", "set_state", "finalize", "add_agent",
                   "update_from_tool_start", "update_from_tool_result"):
        assert hasattr(tracker, method), f"AgentStatusTracker missing {method}"


def test_make_status_panel_returns_null_without_edit_fn():
    panel = make_status_panel("cli", ["A"], "A")
    assert isinstance(panel, NullStatusPanel)


def test_make_status_panel_returns_discord_stub():
    panel = make_status_panel("discord", ["A"], "A")
    assert isinstance(panel, DiscordStatusPanel)


def test_make_status_panel_returns_matrix_stub():
    panel = make_status_panel("matrix", ["A"], "A")
    assert isinstance(panel, MatrixStatusPanel)


def test_make_status_panel_returns_tracker_with_edit_fn():
    """When edit_fn and send_and_get_id_fn are provided, use the real tracker."""
    from nanobot.groupchat.orchestra.broadcast import AgentStatusTracker

    async def fake_send(text: str) -> int | None:
        return 1

    async def fake_edit(msg_id: int, text: str) -> None:
        pass

    panel = make_status_panel("telegram", ["A"], "A", edit_fn=fake_edit, send_and_get_id_fn=fake_send)
    assert isinstance(panel, AgentStatusTracker)
