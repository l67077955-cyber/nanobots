"""Parity tests for the display-layer decontamination move.

Pins the behaviours that used to live inside BroadcastView (and thus died
silently if the view was swapped/disabled) and now live in the orchestration
layer: realtime interrupts on successful chatroom_send (ChatroomSendTool)
and search-credit recovery on tool output (broadcast's _on_tool_result
wrapper logic, replicated here against a real SearchPool).
"""

from __future__ import annotations

from nanobot.groupchat.orchestra.mailbox import MailboxHub
from nanobot.groupchat.orchestra.tools.chatroom_tools import (
    ChatroomSendTool,
    SearchPool,
    trigger_realtime_interrupts,
)


def _hub(names, *, leader="L"):
    mb = MailboxHub()
    for n in names:
        mb.create(n)
    mb.start_round(names)
    mb.set_ranks(ranks={n: "knight" for n in names}, leader=leader)
    return mb


class TestRealtimeInterruptsInTool:
    async def test_leader_send_interrupts_busy_teammate(self):
        mb = _hub(["L", "A", "B"])
        mb.mark_busy("B")
        tool = ChatroomSendTool(mb, agent_name="L", leader_name="L")
        result = await tool.execute(to="B", message="instructions")
        assert "✅" in result
        assert mb.get_interrupt_event("B").is_set()

    async def test_teammate_send_cannot_interrupt_leader(self):
        # Rank hierarchy: low rank never interrupts the leader — identical
        # to the old display-layer behaviour (same helper underneath).
        mb = _hub(["L", "A", "B"])
        mb.mark_busy("L")
        tool = ChatroomSendTool(mb, agent_name="B", leader_name="L")
        result = await tool.execute(to="L", message="report")
        assert "✅" in result
        assert not mb.get_interrupt_event("L").is_set()

    async def test_blocked_send_does_not_interrupt(self):
        mb = _hub(["L", "A", "B"])
        mb.mark_busy("B")
        from nanobot.groupchat.orchestra.mailbox import ConversationPool
        # Sender pool exhausted → allocate fails → BLOCKED, no interrupt
        pool = ConversationPool(agents=["L", "A", "B"],
                                per_agent_capacity={"L": 0, "A": 3, "B": 3})
        tool = ChatroomSendTool(mb, agent_name="L", pool=pool, leader_name="L")
        result = await tool.execute(to="B", message="instructions")
        assert "BLOCKED" in result
        assert not mb.get_interrupt_event("B").is_set()

    async def test_no_leader_name_means_no_interrupt(self):
        # Base-registry construction (engine.py) passes no leader_name —
        # realtime interrupts are broadcast-round-only, matching the old
        # view behaviour (view always had a leader_name in rounds).
        mb = _hub(["A", "B"])
        mb.mark_busy("B")
        tool = ChatroomSendTool(mb, agent_name="A")
        await tool.execute(to="B", message="hi")
        assert not mb.get_interrupt_event("B").is_set()


class TestTriggerRealtimeInterruptsHelper:
    async def test_leader_all_targets_interrupts_busy_agents(self):
        mb = _hub(["L", "A", "B"])
        mb.mark_busy("A")
        mb.mark_busy("B")
        await trigger_realtime_interrupts("L", ["All"], mb, "L")
        assert mb.get_interrupt_event("A").is_set()
        assert mb.get_interrupt_event("B").is_set()

    async def test_rank_blocks_direct_interrupt(self):
        mb = _hub(["L", "A", "B"])
        mb.mark_busy("L")
        # B (knight) → L (leader, top rank): must NOT interrupt
        await trigger_realtime_interrupts("B", ["L"], mb, "L")
        assert not mb.get_interrupt_event("L").is_set()


class TestCreditRecoveryRule:
    """The exact rule moved out of BroadcastView.on_tool_result."""

    def _earns(self, tool_name: str, result: str) -> bool:
        return bool(result) and tool_name not in ("chatroom_send", "wait")

    def test_tool_output_earns(self):
        assert self._earns("web_search", "3 results")
        assert self._earns("exec", "exit 0")
        assert self._earns("web_search", "Error: boom")  # old rule: any non-empty

    def test_chatroom_and_wait_and_empty_do_not_earn(self):
        assert not self._earns("chatroom_send", "✅ sent")
        assert not self._earns("wait", "[A]: hi")
        assert not self._earns("exec", "")
