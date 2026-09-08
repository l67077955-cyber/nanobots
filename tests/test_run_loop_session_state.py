"""Behavioral tests pinning run_loop's session-exit semantics.

Phase 1 step 3 of docs/plan-2026-09-07-arch-refactor.md: the session-loop
condition no longer relies on round code flipping ``engine._running`` mid-
round. Instead ``broadcast_round`` returns a ``RoundResult`` whose
``session_should_stop`` (derived from RoundLifecycle's end reason) tells
run_loop whether the session ends. These tests pin:

- a user message that arrives while the round is winding down (queued by
  _user_listener instead of delivered into the live round) must revive the
  loop for one more round rather than be silently dropped;
- a stopping round with nothing queued exits after exactly one round;
- a non-stopping round (leaderless convergence keeps the session alive)
  flows the next queued message through the normal while-loop;
- /stop (the session-level flag flipped off by the engine's stop path
  while run_loop is waiting for input) exits without running a round.

Uses a minimal duck-typed fake engine (the same "real function + tiny fake"
pattern as tests/test_user_ingress.py) and monkeypatches ``broadcast_round``
— the one expensive/unrelated seam — to simulate round outcomes directly.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.runtime.broadcast import RoundResult
from nanobot.groupchat.runtime.run_loop import run_loop


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
    """REGRESSION (run_loop revival semantics): a round can end with a
    session-stop verdict (leader end_discussion) while a user message is
    mid-flight; the message lands in the queue *after* the round already
    decided to stop. It must not be dropped — the loop should revive for
    exactly one more round to process it."""
    engine = _FakeEngine(["Alpha", "Beta"])
    engine._task = asyncio.current_task()
    engine._input_queue.put_nowait("先聊聊")

    calls: list[list[str]] = []

    async def fake_broadcast_round(speak_order, eng, mailbox, global_timeout=600.0):
        calls.append(list(speak_order))
        if len(calls) == 1:
            # A message that arrived during teardown gets requeued (never
            # delivered into the live round) — simulate that here.
            eng._input_queue.put_nowait("迟到的消息")
        return RoundResult(messages=[], session_should_stop=True)

    monkeypatch.setattr(
        "nanobot.groupchat.runtime.run_loop.broadcast_round", fake_broadcast_round
    )

    await run_loop(engine)

    assert len(calls) == 2, "pending message must trigger a revived second round, not be dropped"
    assert engine._running is False
    assert engine._input_queue.empty()
    assert ("用户", "先聊聊") in engine.added
    assert ("用户", "迟到的消息") in engine.added


@pytest.mark.asyncio
async def test_clean_exit_without_pending_message_does_not_revive(monkeypatch):
    """Guard against over-firing: when the round reports session-should-stop
    with nothing queued, the loop must exit after exactly one round, not
    loop forever."""
    engine = _FakeEngine(["Alpha", "Beta"])
    engine._task = asyncio.current_task()
    engine._input_queue.put_nowait("先聊聊")

    calls: list[list[str]] = []

    async def fake_broadcast_round(speak_order, eng, mailbox, global_timeout=600.0):
        calls.append(list(speak_order))
        return RoundResult(messages=[], session_should_stop=True)

    monkeypatch.setattr(
        "nanobot.groupchat.runtime.run_loop.broadcast_round", fake_broadcast_round
    )

    await run_loop(engine)

    assert len(calls) == 1
    assert engine._running is False
    assert engine._input_queue.empty()


@pytest.mark.asyncio
async def test_ongoing_session_continues_without_revival_when_verdict_keeps_alive(monkeypatch):
    """When a round ends with session_should_stop=False (e.g. leaderless
    convergence, which keeps the session alive per RoundLifecycle), the next
    queued message must flow through the normal while-loop — the revival
    branch should never fire because it isn't needed."""
    engine = _FakeEngine(["Alpha", "Beta"])
    engine._task = asyncio.current_task()
    engine._input_queue.put_nowait("第一轮")

    calls: list[list[str]] = []

    async def fake_broadcast_round(speak_order, eng, mailbox, global_timeout=600.0):
        calls.append(list(speak_order))
        if len(calls) == 1:
            # Session stays alive; queue the next user message as if it
            # arrived normally between rounds (not during teardown).
            eng._input_queue.put_nowait("第二轮")
            return RoundResult(messages=[], session_should_stop=False)
        return RoundResult(messages=[], session_should_stop=True)

    monkeypatch.setattr(
        "nanobot.groupchat.runtime.run_loop.broadcast_round", fake_broadcast_round
    )

    await run_loop(engine)

    assert len(calls) == 2
    assert ("用户", "第一轮") in engine.added
    assert ("用户", "第二轮") in engine.added


@pytest.mark.asyncio
async def test_stop_while_waiting_for_input_exits_without_a_round(monkeypatch):
    """/stop flips the session-level flag from outside while run_loop is
    parked on the empty input queue. The polling wait must notice it and
    exit — without ever calling broadcast_round."""
    engine = _FakeEngine(["Alpha", "Beta"])

    async def must_not_run(*args, **kwargs):
        raise AssertionError("broadcast_round must not run after /stop")

    monkeypatch.setattr(
        "nanobot.groupchat.runtime.run_loop.broadcast_round", must_not_run
    )

    task = asyncio.create_task(run_loop(engine))
    engine._task = task
    await asyncio.sleep(0.2)  # let run_loop park on the empty queue
    engine._running = False   # the engine stop path flips the session flag

    await asyncio.wait_for(task, timeout=3)  # inner poll cadence is 1.0s
    assert engine._running is False
