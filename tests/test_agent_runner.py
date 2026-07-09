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
        self._interrupt_counts: dict[str, int] = {}
        self._try_interrupt_calls: list[tuple[str, str]] = []
        self._try_interrupt_ret = True

    def get_interrupt_event(self, name: str) -> asyncio.Event:
        if name not in self._events:
            self._events[name] = asyncio.Event()
        return self._events[name]

    def mark_busy(self, name: str) -> None:
        self._busy_agents.add(name)

    def mark_idle(self, name: str) -> None:
        self._busy_agents.discard(name)

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
    assert r.is_waiting is False
    assert r.interrupt_pending is False

    # Use runner's method to change state (it owns the state)
    r.begin_cycle()
    assert r.state == "busy"
    assert r.is_busy is True
    assert "Kirk" in mb._busy_agents  # mailbox kept in sync

    # waiting is a detail of idle (no tool_loop in flight)
    r.end_cycle()
    mb._waiting.add("Kirk")
    assert r.state == "idle"
    assert r.is_waiting is True
    mb._waiting.discard("Kirk")
    assert r.state == "idle"
    assert r.is_waiting is False

    # interrupt is a momentary event, not a state tier
    r.begin_cycle()
    r.interrupt_event.set()
    assert r.state == "busy"  # still busy (tool_loop racing interrupt)
    r.end_cycle()
    assert r.state == "idle"
    assert r.interrupt_pending is True  # detail of idle

    # task done overrides everything
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


# ── Cycle state machine (Step 3) ──────────────────────────────────────────


def test_begin_end_cycle_drives_busy_state():
    mb = _StubMailbox()
    r = AgentRunner("Kirk", mb, lambda: _FakeTask(done=False))
    assert not r.is_busy
    r.begin_cycle()
    assert r.is_busy
    assert r.state == "busy"
    r.end_cycle()
    assert not r.is_busy
    assert r.state == "idle"


def test_acknowledge_interrupt_clears_event_and_resets_count():
    mb = _StubMailbox()
    r = AgentRunner("Kirk", mb, lambda: _FakeTask(done=False))
    # Simulate two interrupts setting event + counter (as mailbox._try_interrupt would)
    r.interrupt_event.set()
    mb._interrupt_counts["Kirk"] = 2
    r.begin_cycle()
    assert r.state == "busy"
    assert r.interrupt_pending is True

    r.acknowledge_interrupt()

    assert not r.interrupt_event.is_set()
    assert mb._interrupt_counts["Kirk"] == 0
    assert r.interrupt_pending is False


def test_acknowledge_interrupt_idempotent_on_clean_state():
    mb = _StubMailbox()
    r = AgentRunner("Kirk", mb, lambda: _FakeTask(done=False))
    # Nothing set — acknowledge is a no-op, must not raise.
    r.acknowledge_interrupt()
    assert not r.interrupt_event.is_set()
    assert mb._interrupt_counts["Kirk"] == 0

