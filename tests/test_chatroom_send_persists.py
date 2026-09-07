"""Phase C regression tests: chatroom_send persists to the shared log.

Pins that an agent-to-agent ``chatroom_send`` writes a targets-tagged record
into the persistent log (``engine._add_message``), so the message:

  - survives across rounds even when the mailbox queue is cleared at
    ``start_round`` (the core 跨轮留存 fix — previously a failed interrupt
    permanently lost the message and it was in no history), AND
  - respects per-agent visibility — a B-targeted send never reaches C's view.

Without this, agent inter-messages lived only in the ephemeral mailbox; once
``start_round`` reset the queues (or an interrupt failed because same-rank
agents can't interrupt each other), the message was gone and not in any
agent's prompt history.
"""

from __future__ import annotations

from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.orchestra.mailbox import MailboxHub
from nanobot.groupchat.orchestra.tools.chatroom_tools import ChatroomSendTool


def _make_engine(tmp_path):
    """A lightweight GroupChatEngine stub with a real HistoryContext.

    Binds the real (unbound) ``_add_message`` so the test exercises the actual
    engine persistence path (history.add_message + shim sync) without building
    providers / MCP / config.
    """
    from nanobot.groupchat.orchestra.engine import GroupChatEngine

    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    ctx = HistoryContext(state=state, provider=None)
    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine.history = ctx
    engine._history = ctx.messages
    return engine


def _make_mailbox(names):
    mb = MailboxHub()
    for n in names:
        mb.create(n)
    mb.start_round(names)
    return mb


# ─────────────────────────────────────────────────────────────────────────
# Persistence + visibility
# ─────────────────────────────────────────────────────────────────────────


class TestChatroomSendPersists:
    async def test_private_send_persisted_with_target_visibility(self, tmp_path):
        """A→B send is persisted with targets=[B]; C never sees it."""
        engine = _make_engine(tmp_path)
        mb = _make_mailbox(["A", "B", "C"])
        tool = ChatroomSendTool(mailbox=mb, agent_name="A", engine=engine)

        result = await tool.execute(to="B", message="secret for B")
        assert "sent to B" in result

        last = engine.history.messages[-1]
        assert last["sender"] == "A"
        assert last["content"] == "secret for B"
        assert last["targets"] == ["B"]

        assert any(m["content"] == "secret for B" for m in engine.history.view_for("A"))
        assert any(m["content"] == "secret for B" for m in engine.history.view_for("B"))
        assert not any(m["content"] == "secret for B" for m in engine.history.view_for("C"))

    async def test_broadcast_send_persisted_as_all(self, tmp_path):
        engine = _make_engine(tmp_path)
        mb = _make_mailbox(["A", "B", "C"])
        tool = ChatroomSendTool(mailbox=mb, agent_name="A", engine=engine)

        await tool.execute(to="All", message="team update")
        last = engine.history.messages[-1]
        assert last["targets"] == ["All"]
        for name in ("A", "B", "C"):
            assert any(m["content"] == "team update" for m in engine.history.view_for(name))

    async def test_multi_target_send_persisted(self, tmp_path):
        engine = _make_engine(tmp_path)
        mb = _make_mailbox(["A", "B", "C"])
        tool = ChatroomSendTool(mailbox=mb, agent_name="A", engine=engine)

        await tool.execute(to=["B", "C"], message="for both")
        last = engine.history.messages[-1]
        assert last["targets"] == ["B", "C"]
        for name in ("A", "B", "C"):
            assert any(m["content"] == "for both" for m in engine.history.view_for(name))

    async def test_persisted_message_keeps_chronological_order(self, tmp_path):
        engine = _make_engine(tmp_path)
        mb = _make_mailbox(["A", "B"])
        tool = ChatroomSendTool(mailbox=mb, agent_name="A", engine=engine)
        engine._add_message("用户", "question")
        await tool.execute(to="B", message="answer")
        senders = [m["sender"] for m in engine.history.messages]
        assert senders == ["用户", "A"]


# ─────────────────────────────────────────────────────────────────────────
# Cross-round survival — the core fix
# ─────────────────────────────────────────────────────────────────────────


class TestCrossRoundPersistence:
    async def test_message_survives_mailbox_round_reset(self, tmp_path):
        """REGRESSION: mailbox.start_round clears queues, but a chatroom_send
        message must remain in the persistent log so the recipient still sees
        it next round.  Before Phase C the message lived only in the mailbox
        and was lost on round reset."""
        engine = _make_engine(tmp_path)
        mb = _make_mailbox(["A", "B", "C"])
        tool = ChatroomSendTool(mailbox=mb, agent_name="A", engine=engine)
        await tool.execute(to="B", message="don't forget this")

        # Round boundary: mailbox resets queues
        mb.start_round(["A", "B", "C"])
        assert mb._queues["B"].empty()  # mailbox queue is cleared…

        # …but the persistent view for B still contains the message
        assert any(m["content"] == "don't forget this" for m in engine.history.view_for("B"))
        # …and C still does not
        assert not any(m["content"] == "don't forget this" for m in engine.history.view_for("C"))


# ─────────────────────────────────────────────────────────────────────────
# Back-compat: engine=None (older construction sites) still delivers
# ─────────────────────────────────────────────────────────────────────────


class TestNoEngineBackcompat:
    async def test_send_without_engine_still_delivers(self, tmp_path):
        """A ChatroomSendTool built without an engine ref still delivers via
        mailbox — it just doesn't persist.  Keeps existing construction sites
        (engine.py default registries, test_view_purity) working."""
        mb = _make_mailbox(["A", "B"])
        tool = ChatroomSendTool(mailbox=mb, agent_name="A", engine=None)
        result = await tool.execute(to="B", message="no engine")
        assert "sent to B" in result
        assert not mb._queues["B"].empty()
