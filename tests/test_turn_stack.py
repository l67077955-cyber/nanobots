"""Unit tests for TurnStack (Step 2)."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.runtime.turn_stack import TurnStack


class _StubMailbox:
    def __init__(self) -> None:
        self.agent_names = ["Kirk", "Harper"]
        self.created: list[str] = []
        self.sends: list[tuple[str, list[str], str]] = []
        self.interrupted = 0
        self._discussion_ended = False

    def is_discussion_ended(self) -> bool:
        return self._discussion_ended

    def create(self, name: str) -> None:
        self.created.append(name)

    def send(self, sender: str, targets: list[str], content: str) -> int:
        self.sends.append((sender, targets, content))
        return len(targets)

    def interrupt_busy_agents(self, sender: str) -> int:
        self.interrupted += 1
        return 2


class _StubPool:
    def __init__(self) -> None:
        self.allocations = 0

    async def allocate_user(self, recipients: list[str]) -> None:
        self.allocations += 1

    def status(self) -> str:
        return "pool-status"


class _StubEngine:
    """Minimal GroupChatEngine surface used by TurnStack."""

    def __init__(self, running: bool = True) -> None:
        self._running = running
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._broadcast_tasks: dict[str, asyncio.Task] = {}
        self.added: list[tuple[str, str]] = []
        self.sends: list[str] = []

    def _add_message(self, sender: str, content: str) -> None:
        self.added.append((sender, content))

    async def _send(self, msg: str) -> None:
        self.sends.append(msg)


@pytest.mark.asyncio
async def test_interject_injects_on_live_round():
    engine = _StubEngine(running=True)
    mb = _StubMailbox()
    pool = _StubPool()
    stack = TurnStack(engine, mb, pool, ["Kirk", "Harper"])

    injected = await stack.interject("hi")

    assert injected is True
    assert pool.allocations == 1
    assert mb.created == ["用户"]
    assert mb.sends == [("用户", ["All"], "hi")]
    assert mb.interrupted == 1
    assert engine.added == [("用户", "hi")]
    assert engine.sends and "── User ──" in engine.sends[0]
    assert engine._input_queue.empty()


@pytest.mark.asyncio
async def test_interject_requeues_when_round_winding_down():
    engine = _StubEngine(running=False)  # engine stopped → winding down
    mb = _StubMailbox()
    pool = _StubPool()
    stack = TurnStack(engine, mb, pool, ["Kirk", "Harper"])

    injected = await stack.interject("late")

    assert injected is False
    # Requeued for the next round, NOT injected.
    assert not engine._input_queue.empty()
    assert engine._input_queue.get_nowait() == "late"
    assert mb.sends == []
    assert pool.allocations == 0
    assert engine.added == []


@pytest.mark.asyncio
async def test_interject_requeues_when_all_tasks_done():
    engine = _StubEngine(running=True)
    # All broadcast tasks done → winding down.
    done_task = asyncio.get_event_loop().create_task(asyncio.sleep(0))
    await done_task
    engine._broadcast_tasks["Kirk"] = done_task
    mb = _StubMailbox()
    pool = _StubPool()
    stack = TurnStack(engine, mb, pool, ["Kirk"])

    injected = await stack.interject("end")

    assert injected is False
    assert not engine._input_queue.empty()
    assert mb.sends == []


@pytest.mark.asyncio
async def test_interject_requeues_when_discussion_ended():
    engine = _StubEngine(running=True)
    mb = _StubMailbox()
    mb._discussion_ended = True
    pool = _StubPool()
    stack = TurnStack(engine, mb, pool, ["Kirk", "Harper"])

    injected = await stack.interject("ended")

    assert injected is False
    assert mb.sends == []


@pytest.mark.asyncio
async def test_cancel_all_cancels_live_tasks():
    engine = _StubEngine(running=True)

    async def _long() -> None:
        await asyncio.sleep(100)

    t1 = asyncio.create_task(_long())
    t2 = asyncio.create_task(_long())
    engine._broadcast_tasks["Kirk"] = t1
    engine._broadcast_tasks["Harper"] = t2

    stack = TurnStack(engine, _StubMailbox(), None, ["Kirk", "Harper"])
    count = stack.cancel_all()

    assert count == 2
    await asyncio.sleep(0)
    assert t1.done() and t2.done()
    for t in (t1, t2):
        with pytest.raises(asyncio.CancelledError):
            await t


def test_cancel_all_skips_done_tasks():
    engine = _StubEngine(running=True)

    class _Done:
        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            raise AssertionError("should not cancel a done task")

    engine._broadcast_tasks["Kirk"] = _Done()  # type: ignore[assignment]
    stack = TurnStack(engine, _StubMailbox(), None, ["Kirk"])
    assert stack.cancel_all() == 0


def test_active_agents_returns_copy():
    engine = _StubEngine()
    names = ["Kirk", "Harper"]
    stack = TurnStack(engine, _StubMailbox(), None, names)
    assert stack.active_agents == names
    # Mutating the returned list must not affect the stack.
    stack.active_agents.append("X")
    assert stack.active_agents == names
