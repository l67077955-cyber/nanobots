"""Unit tests for AgentRunner facade (Step 0.5)."""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.orchestra.agent_runner import AgentRunner


class _StubMailbox:
    """Minimal MailboxHub surface used by AgentRunner (delegating facade)."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._busy_agents: set[str] = set()
        self._waiting: set[str] = set()
        self._last_interrupt_sender: dict[str, str] = {}
        self._try_interrupt_calls: list[tuple[str, str]] = []
        self._try_interrupt_ret = True

    def get_interrupt_event(self, name: str) -> asyncio.Event:
        if name not in self._events:
            self._events[name] = asyncio.Event()
        return self._events[name]

    def _try_interrupt(self, target: str, sender: str) -> bool:
        self._try_interrupt_calls.append((target, sender))
        return self._try_interrupt_ret


class _FakeTask:
    """Stand-in asyncio.Task with just the .done() surface AgentRunner uses."""

    def __init__(self, done: bool = False) -> None:
        self._done = done
        self.cancelled = False

    def done(self) -> bool:  # type: ignore[override]
        return self._done

    def cancel(self) -> bool:
        self.cancelled = True
        return True


def test_interrupt_event_is_mailbox_event():
    mb = _StubMailbox()
    r = AgentRunner("Kirk", mb, lambda: None)
    assert r.interrupt_event is mb.get_interrupt_event("Kirk")


def test_force_interrupt_sets_event_and_attribution():
    mb = _StubMailbox()
    r = AgentRunner("Kirk", mb, lambda: None)
    assert r.force_interrupt("用户", "测试") is True
    assert r.interrupt_event.is_set()
    assert mb._last_interrupt_sender["Kirk"] == "用户"
    # Already set → idempotent False, no double-attribution overwrite.
    assert r.force_interrupt("队友", "again") is False
    assert mb._last_interrupt_sender["Kirk"] == "用户"


def test_request_interrupt_delegates_with_rank():
    mb = _StubMailbox()
    r = AgentRunner("Kirk", mb, lambda: None)
    assert r.request_interrupt("Harper") is True
    assert mb._try_interrupt_calls == [("Kirk", "Harper")]
    mb._try_interrupt_ret = False
    assert r.request_interrupt("Harper") is False


def test_state_derivation():
    mb = _StubMailbox()
    holder = {"t": None}
    r = AgentRunner("Kirk", mb, lambda: holder["t"])

    # No task → done
    assert r.state == "done"

    task = _FakeTask(done=False)
    holder["t"] = task
    assert r.state == "idle"
    assert r.is_busy is False

    mb._busy_agents.add("Kirk")
    assert r.state == "busy"
    assert r.is_busy is True

    # waiting takes precedence over busy
    mb._waiting.add("Kirk")
    assert r.state == "waiting"
    mb._waiting.discard("Kirk")
    assert r.state == "busy"

    # interrupted takes precedence over everything (task still alive)
    r.interrupt_event.set()
    assert r.state == "interrupted"

    # task done overrides interrupt
    task._done = True
    assert r.state == "done"


@pytest.mark.asyncio
async def test_cancel_sets_event_and_cancels_task():
    mb = _StubMailbox()

    async def _long() -> None:
        await asyncio.sleep(100)

    task = asyncio.create_task(_long())
    r = AgentRunner("Kirk", mb, lambda: task)

    r.cancel("stop")
    assert r.interrupt_event.is_set()
    # Let the cancellation propagate.
    await asyncio.sleep(0)
    assert task.done()

    with pytest.raises(asyncio.CancelledError):
        await task
