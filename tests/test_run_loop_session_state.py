"""Behavioral tests pinning run_loop's session-level use of ``engine._running``.

Phase 1 of plan.md ("状态所有权收编") will migrate the session-loop condition
in ``run_loop`` off the raw ``engine._running`` bool onto an explicit session
state source. Before that migration touches anything, these tests pin the
current behavior described in the run_loop.py:154-164 comment: a user
message that arrives while the round is winding down (queued by
_user_listener instead of delivered into the live round) must revive the
loop for one more round rather than being silently dropped when the round
ends with ``engine._running`` already flipped False.

Uses a minimal duck-typed fake engine (the same "real function + tiny fake"
pattern as tests/test_user_ingress.py) and monkeypatches ``broadcast_round``
— the one expensive/unrelated seam — to simulate round outcomes directly.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.orchestra.run_loop import run_loop


class _FakeHistory:
    def has_system_message(self) -> bool:
        return True


class _FakeEngine:
    def __init__(self, agents):
        self._active_agents = list(agents)
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = True
        self._round = 0
        self._topic = ""
        self._mailbox = None
        self._task = None
        self._on_round_done = None
        self._summary_requested = False
        self.history = _FakeHistory()
        self.sent: list[str] = []
        self.added: list[tuple[str, str]] = []

    def _add_message(self, sender: str, content: str) -> None:
        self.added.append((sender, content))

    async def _send(self, text: str) -> None:
        self.sent.append(text)

    async def _maybe_compress_history(self) -> None:
        pass


@pytest.mark.asyncio
async def test_pending_message_after_teardown_revives_loop_instead_of_dropping(monkeypatch):
    """REGRESSION (run_loop.py:154-164): end_discussion can flip
    engine._running off while a user message is mid-flight; the message
    lands in the queue *after* the round already decided to stop. It must
    not be dropped — the loop should revive for exactly one more round to
    process it."""
    engine = _FakeEngine(["Alpha", "Beta"])
    engine._input_queue.put_nowait("先聊聊")

    calls: list[list[str]] = []

    async def fake_broadcast_round(speak_order, eng, mailbox, global_timeout=600.0):
        calls.append(list(speak_order))
        if len(calls) == 1:
            # end_discussion flips _running off mid-round; a message that
            # arrived during teardown gets requeued (never delivered into
            # the live round) — simulate that here.
            eng._running = False
            eng._input_queue.put_nowait("迟到的消息")
        else:
            eng._running = False
        return []

    monkeypatch.setattr(
        "nanobot.groupchat.orchestra.run_loop.broadcast_round", fake_broadcast_round
    )

    await run_loop(engine)

    assert len(calls) == 2, "pending message must trigger a revived second round, not be dropped"
    assert engine._running is False
    assert engine._input_queue.empty()
    assert ("用户", "先聊聊") in engine.added
    assert ("用户", "迟到的消息") in engine.added


@pytest.mark.asyncio
async def test_clean_exit_without_pending_message_does_not_revive(monkeypatch):
    """Guard against over-firing: when the round ends with nothing queued,
    the loop must exit after exactly one round, not loop forever."""
    engine = _FakeEngine(["Alpha", "Beta"])
    engine._input_queue.put_nowait("先聊聊")

    calls: list[list[str]] = []

    async def fake_broadcast_round(speak_order, eng, mailbox, global_timeout=600.0):
        calls.append(list(speak_order))
        eng._running = False
        return []

    monkeypatch.setattr(
        "nanobot.groupchat.orchestra.run_loop.broadcast_round", fake_broadcast_round
    )

    await run_loop(engine)

    assert len(calls) == 1
    assert engine._running is False
    assert engine._input_queue.empty()


@pytest.mark.asyncio
async def test_ongoing_session_continues_without_revival_when_still_running(monkeypatch):
    """When a round ends with engine._running still True (e.g. leaderless
    convergence, which keeps the session alive per RoundLifecycle.session_
    should_stop), the next queued message must flow through the normal
    while-loop — the revival branch should never fire because it isn't
    needed."""
    engine = _FakeEngine(["Alpha", "Beta"])
    engine._input_queue.put_nowait("第一轮")

    calls: list[list[str]] = []

    async def fake_broadcast_round(speak_order, eng, mailbox, global_timeout=600.0):
        calls.append(list(speak_order))
        if len(calls) == 1:
            # Session stays alive; queue the next user message as if it
            # arrived normally between rounds (not during teardown).
            eng._input_queue.put_nowait("第二轮")
        else:
            eng._running = False
        return []

    monkeypatch.setattr(
        "nanobot.groupchat.orchestra.run_loop.broadcast_round", fake_broadcast_round
    )

    await run_loop(engine)

    assert len(calls) == 2
    assert ("用户", "第一轮") in engine.added
    assert ("用户", "第二轮") in engine.added
