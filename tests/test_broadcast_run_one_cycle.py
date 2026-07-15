"""Integration tests for CycleController wiring in _run_one.

These tests exercise the full cycle-loop decision flow with stubbed
tool_loop, mailbox, and engine. The CycleController oracle is pure and
already unit-tested (test_cycle_controller.py); these tests verify that
the wiring correctly passes state snapshots to the oracle and respects
its action decisions.

Key paths (from the risk audit):
(a) Normal: cycle 1 → content → auto-wait → teammate msg → cycle 2 → done
(b) Leader end_discussion H1→H2→break (no text → force synthesis → display → break)
(c) Timeout C1 first retry succeeds (returns content, continue to auto-wait)
(d) Timeout C2 retry fails (force placeholder, continue to auto-wait)
(e) Error C4 after 3 consecutive errors (leader vs non-leader exit behavior)
(f) max_cycles A breach (forced synthesis+exit)
(g) Interrupt D (drain queue, inject, re-enter tool_loop)
(h) C3 fall-through (repeat timeout with empty retry → error-state body runs + auto-wait)

The stub harness:
- _FakeToolLoop: returns configurable ToolLoopResult on each call
- _StubMailbox: minimal MailboxHub surface (queues, wait, busy/waiting sets, interrupt event)
- _StubEngine: minimal GroupChatEngine surface (running, registry, provider, send/add_message)
- _NoopTracker: set_state no-op
- _NoopStream: StreamingDisplay stub
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest


# ── Minimal result object mirroring tool_loop.ToolLoopResult ─────────────

@dataclass
class FakeToolLoopResult:
    """Configurable result returned by _FakeToolLoop."""
    content: str = ""
    finish_reason: str = "stop"  # stop|error|timeout|interrupted|end_discussion|max_iterations
    tools_used: list[str] = field(default_factory=list)
    tool_calls_detail: list[dict[str, Any]] = field(default_factory=list)
    latency: float = 0.5
    token_usage: dict[str, int] = field(default_factory=lambda: {"prompt": 100, "completion": 50, "total": 150})
    cost: float = 0.01
    cache_tokens: int = 0
    provider_meta: list[dict] = field(default_factory=list)
    iterations: int = 1


# ── Fake tool_loop callable ───────────────────────────────────────────────

class _FakeToolLoop:
    """Returns a queue of pre-configured results for each call.

    Usage:
        fake = _FakeToolLoop([
            FakeToolLoopResult(finish_reason="timeout"),
            FakeToolLoopResult(content="retry success"),
        ])
        # first call -> timeout, second call -> success
    """
    def __init__(self, results: list[FakeToolLoopResult]) -> None:
        self._results = list(results)
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []  # record of kwargs for each call

    async def __call__(self, **kwargs: Any) -> FakeToolLoopResult:
        self.call_count += 1
        self.calls.append(kwargs)
        if self._results:
            return self._results.pop(0)
        return FakeToolLoopResult()  # default after exhaustion


# ── Minimal MailboxHub stub ───────────────────────────────────────────────

class _StubMailbox:
    """Minimal MailboxHub surface needed by _run_one cycle loop."""
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._busy_agents: set[str] = set()
        self._waiting: set[str] = set()
        self._interrupt_events: dict[str, asyncio.Event] = {}
        self._discussion_ended = False

    def create(self, name: str) -> None:
        if name not in self._queues:
            self._queues[name] = asyncio.Queue()
        self._interrupt_events[name] = asyncio.Event()

    def get_interrupt_event(self, name: str) -> asyncio.Event:
        return self._interrupt_events.setdefault(name, asyncio.Event())

    def mark_busy(self, name: str) -> None:
        self._busy_agents.add(name)

    def mark_idle(self, name: str) -> None:
        self._busy_agents.discard(name)

    def busy_agents(self) -> set[str]:
        return set(self._busy_agents)

    def send(self, sender: str, targets: list[str], content: str) -> None:
        for t in targets:
            if t in self._queues:
                self._queues[t].put_nowait(_AgentMessage(sender, content))

    async def wait(self, name: str, timeout: float = 600) -> _AgentMessage | None:
        """Simplified wait: blocks until queue has message or timeout.

        Does NOT implement busy-replier extensions or all-waiting detection.
        """
        try:
            return await asyncio.wait_for(self._queues[name].get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def mark_agent_done(self, name: str) -> None:
        self._waiting.discard(name)

    def is_discussion_ended(self) -> bool:
        return self._discussion_ended

    def mark_discussion_ended(self) -> None:
        self._discussion_ended = True
        for evt in self._interrupt_events.values():
            evt.set()


@dataclass
class _AgentMessage:
    sender: str
    content: str


# ── Minimal Engine stub ───────────────────────────────────────────────────

class _StubEngine:
    """Minimal GroupChatEngine surface needed by _run_one."""
    def __init__(self, running: bool = True) -> None:
        self._running = running
        self._broadcast_tasks: dict[str, asyncio.Task] = {}
        self._runners: dict[str, Any] = {}
        self.registry: dict[str, dict[str, Any]] = {}
        self.provider: Any = None  # not used in cycle decision tests
        self.config: Any = _StubConfig()
        self._add_message_calls: list[tuple[str, str]] = []
        self._send_calls: list[str] = []
        self.stream_replies = False
        self._active_stream: Any = None

    def _get_agent_tools(self, cfg: dict, reg: Any, agent_name: str) -> None:
        return None  # no tools in cycle decision tests

    def _build_agent_prompt(self, *a: Any, **kw: Any) -> list[dict]:
        return [{"role": "user", "content": "test prompt"}]

    def _add_message(self, name: str, content: str) -> None:
        self._add_message_calls.append((name, content))

    async def _send(self, msg: str) -> None:
        self._send_calls.append(msg)

    def register_active_stream(self, stream: Any) -> None:
        self._active_stream = stream

    def unregister_active_stream(self, stream: Any) -> None:
        if self._active_stream is stream:
            self._active_stream = None


class _StubConfig:
    max_tokens: int = 2000


# ── Noop tracker / stream ──────────────────────────────────────────────────

class _NoopTracker:
    async def set_state(self, name: str, state: str, **kw: Any) -> None:
        pass


class _NoopStream:
    enabled: bool = False

    @property
    def on_delta(self) -> None:
        return None

    @property
    def on_reset(self) -> None:
        return None

    async def finalize(self, content: str, **kw: Any) -> None:
        pass


# ── Test: sanity check that stubs assemble ──────────────────────────────────

@pytest.mark.asyncio
async def test_stub_harness_assembles():
    """Verify the stub harness can be constructed without errors."""
    mailbox = _StubMailbox()
    mailbox.create("TestAgent")
    engine = _StubEngine()
    tracker = _NoopTracker()

    # Simulate a minimal cycle-loop state
    evt = mailbox.get_interrupt_event("TestAgent")
    assert not evt.is_set()

    mailbox.mark_busy("TestAgent")
    assert "TestAgent" in mailbox.busy_agents()

    mailbox.mark_idle("TestAgent")
    assert "TestAgent" not in mailbox.busy_agents()


# ── Placeholder for real integration tests ────────────────────────────────

# The actual wiring tests require monkeypatching `tool_loop` and
# `StreamingDisplay` in the broadcast module, then driving `_run_one`
# through various state sequences. This skeleton establishes the stub
# infrastructure; the 8 paths (a-h) will be added incrementally.
#
# Key insight: CycleController is pure, so the core test pattern is:
# 1. Configure _FakeToolLoop to return a specific finish_reason/content
# 2. Set engine/mailbox state to match the test scenario
# 3. Run one iteration of the cycle loop (or a controlled number)
# 4. Assert the oracle was called with expected context
# 5. Assert the action taken matches the expected behavior

@pytest.mark.asyncio
async def test_cycle_gate_max_cycles_forces_exit():
    """Test that max_cycles breach forces synthesis then exit.

    This is path (f) from the risk audit. When cycle >= max_cycles,
    the gate should return EXIT_MAX_CYCLES_FORCE_SYNTHESIS.

    Note: Full wiring requires monkeypatching _run_one internals.
    This test validates the stub harness + oracle interaction.
    """
    from nanobot.groupchat.runtime.cycle_controller import (
        CycleController, CycleContext, CycleAction,
    )

    ctrl = CycleController("TestAgent")
    ctx = CycleContext(
        agent_name="TestAgent",
        is_leader=True,
        cycle=30,
        max_cycles=30,
        total_agents=3,
        engine_running=True,
        discussion_ended=False,
        leader_ended_discussion=False,
        leader_end_event_set=False,
        finish_reason="stop",
        content="",
        tools_used=(),
        substantive_tools=frozenset({"web_search"}),
        timeout_recovery_count=0,
        consecutive_error_count=0,
        max_consecutive_errors=3,
    )

    decision = ctrl.decide_cycle_gate(ctx)
    assert decision.action is CycleAction.EXIT_MAX_CYCLES_FORCE_SYNTHESIS
