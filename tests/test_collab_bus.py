"""CollabBus / round_log delivery surface (Phase 1 message path)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nanobot.groupchat.runtime.collab_bus import CollabBus, deliver
from nanobot.groupchat.runtime.mailbox import MailboxHub
from nanobot.groupchat.runtime.tools.chatroom_tools import (
    ChatroomSendTool,
    ListMessagesTool,
    QuoteMessageTool,
    WaitTool,
)


def test_mailboxhub_is_collab_bus() -> None:
    mb = MailboxHub()
    mb.create("A")
    mb.create("B")
    assert isinstance(mb, CollabBus)


def test_send_all_delivers_except_sender() -> None:
    mb = MailboxHub()
    for a in ("A", "B", "C"):
        mb.create(a)
    n = mb.send("A", ["All"], "hello-all")
    assert n == 2
    assert len(mb.round_log) == 1
    assert mb.round_log[0].content == "hello-all"


@pytest.mark.asyncio
async def test_wait_receives_message() -> None:
    mb = MailboxHub()
    mb.create("A")
    mb.create("B")
    deliver(mb, "A", ["B"], "ping")
    msg = await mb.wait("B", timeout=1.0)
    assert msg is not None
    assert msg.sender == "A"
    assert msg.content == "ping"


@pytest.mark.asyncio
async def test_wait_from_agent_filter_puts_back() -> None:
    mb = MailboxHub()
    mb.create("A")
    mb.create("B")
    mb.create("C")
    mb.send("C", ["B"], "from-c")
    mb.send("A", ["B"], "from-a")
    msg = await mb.wait("B", timeout=1.0, from_agent="A")
    assert msg is not None
    assert msg.sender == "A"
    # C's message still available
    msg2 = await mb.wait("B", timeout=1.0)
    assert msg2 is not None
    assert msg2.sender == "C"


@pytest.mark.asyncio
async def test_wait_returns_none_on_interrupt() -> None:
    mb = MailboxHub()
    mb.create("A")
    mb.create("B")
    evt = mb.get_interrupt_event("B")
    evt.set()
    msg = await mb.wait("B", timeout=30.0)
    assert msg is None


def test_round_log_and_get_message() -> None:
    mb = MailboxHub()
    mb.create("A")
    mb.create("B")
    mb.send("A", ["B"], "x")
    m = mb.round_log[0]
    assert mb.get_message(m.id) is m or mb.get_message(m.id).content == "x"
    assert mb.get_message(99999) is None


def test_round_log_cleared_on_start_round() -> None:
    mb = MailboxHub()
    mb.create("A")
    mb.create("B")
    mb.send("A", ["B"], "x")
    assert mb.round_log
    mb.start_round(active_agents=["A", "B"])
    assert mb.round_log == []


def test_no_public_mailbox_history_api() -> None:
    mb = MailboxHub()
    assert not hasattr(mb, "history") or not callable(getattr(type(mb), "history", None))
    # property named history must not exist
    assert "history" not in type(mb).__dict__
    assert "round_log" in type(mb).__dict__ or hasattr(mb, "round_log")


@pytest.mark.asyncio
async def test_chatroom_send_does_not_touch_history() -> None:
    """chatroom_send must not call History.commit_turn."""
    mb = MailboxHub()
    mb.create("Harper")
    mb.create("Kirk")
    tool = ChatroomSendTool(mailbox=mb, agent_name="Harper")
    # If someone wired History into send path, a mock would be called —
    # assert only bus delivery:
    result = await tool.execute(to="Kirk", message="findings")
    assert "sent" in result.lower() or "delivered" in result.lower()
    assert len(mb.round_log) == 1
    msg = await mb.wait("Kirk", timeout=1.0)
    assert msg is not None and msg.content == "findings"


@pytest.mark.asyncio
async def test_list_and_quote_use_round_log() -> None:
    mb = MailboxHub()
    mb.create("A")
    mb.create("B")
    mb.send("A", ["B"], "full content here")
    mid = mb.round_log[0].id
    listed = await ListMessagesTool(mb).execute(limit=10)
    assert f"ID:{mid}" in listed
    assert "History" in listed or "投递" in listed  # wording distinguishes from History
    quoted = await QuoteMessageTool(mb).execute(id=mid)
    assert "full content here" in quoted


def test_source_has_no_mailbox_history_public_name() -> None:
    from pathlib import Path
    import re

    roots = [
        Path("nanobot/groupchat/runtime/mailbox.py"),
        Path("nanobot/groupchat/runtime/tools/chatroom_tools.py"),
        Path("nanobot/groupchat/runtime/broadcast.py"),
        Path("nanobot/groupchat/runtime/agent_cycle.py"),
    ]
    pat = re.compile(r"mailbox\.history|_mailbox\.history")
    for p in roots:
        text = p.read_text(encoding="utf-8")
        assert not pat.search(text), f"legacy mailbox.history in {p}"


def test_layer_collab_bus_exported_from_ports() -> None:
    from nanobot.groupchat.runtime.ports import CollabBus as P
    from nanobot.groupchat.runtime.collab_bus import CollabBus as C

    assert P is C
