"""Scenario-replay tests for the 2026-07-19 end-of-discussion stall.

Drives ``run_agent_cycle`` with a scripted ``tool_loop`` through the exact
interleavings observed in production:

- Incident (gateway.log 18:46–18:50): leader's end_discussion succeeded, the
  F/G guard forced a re-entry, and the very next tool_loop call returned
  ``interrupted`` (``mark_discussion_ended()`` sets every agent's interrupt
  event). The old interrupt handler re-entered the loop → 3-minute stall,
  5× end_discussion, 30+ redundant tool calls.
- Fix verification round (21:01:12): same interleaving, the interrupt
  handler now exits immediately → round ended in 177s.

Pinned behaviors:
1. interrupt after discussion ended → exit cycle loop (no re-entry)
2. H1 forced synthesis is bounded (cap=2), then exits even with no text
3. happy path (end_discussion + text in one cycle) unchanged
4. normal mid-round interrupt (discussion NOT ended) still re-enters
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from nanobot.groupchat.runtime.agent_cycle import AgentCycleEnv, run_agent_cycle


# ── Scripted tool_loop ──────────────────────────────────────────────────────

@dataclass
class _Result:
    content: str = ""
    finish_reason: str = "stop"
    tools_used: list[str] = field(default_factory=list)
    tool_calls_detail: list[dict[str, Any]] = field(default_factory=list)
    latency: float = 0.1
    token_usage: dict[str, int] = field(default_factory=lambda: {"prompt": 10, "completion": 5, "total": 15})
    cost: float = 0.0
    cache_tokens: int = 0
    provider_meta: list[dict] = field(default_factory=list)
    iterations: int = 1


class _ScriptedToolLoop:
    """Pops scripted results; runs an optional side effect per call (to
    simulate EndDiscussionTool's engine/mailbox mutations)."""

    def __init__(self, steps: list[_Result], on_call: Callable[[int], None] | None = None) -> None:
        self._steps = list(steps)
        self._on_call = on_call
        self.call_count = 0

    async def __call__(self, **kwargs: Any) -> _Result:
        self.call_count += 1
        if self._on_call:
            self._on_call(self.call_count)
        if self._steps:
            return self._steps.pop(0)
        return _Result(content="unscripted reply")


# ── Stubs ───────────────────────────────────────────────────────────────────

@dataclass
class _Msg:
    sender: str
    content: str


class _StubMailbox:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}
        self._interrupt_events: dict[str, asyncio.Event] = {}
        self._busy_agents: set[str] = set()
        self._discussion_ended = False
        self._interrupt_counts: dict[str, int] = {}
        self._last_interrupt_sender: dict[str, str] = {}
        self.round_log: list[_Msg] = []

    def create(self, name: str) -> None:
        self._queues.setdefault(name, asyncio.Queue())
        self._interrupt_events.setdefault(name, asyncio.Event())

    def get_interrupt_event(self, name: str) -> asyncio.Event:
        return self._interrupt_events.setdefault(name, asyncio.Event())

    def mark_busy(self, name: str) -> None:
        self._busy_agents.add(name)

    def mark_idle(self, name: str) -> None:
        self._busy_agents.discard(name)

    def busy_agents(self) -> set[str]:
        return set(self._busy_agents)

    def send(self, sender: str, targets: list[str], content: str) -> int:
        delivered = 0
        for t in targets:
            if t in self._queues:
                self._queues[t].put_nowait(_Msg(sender, content))
                delivered += 1
        return delivered

    async def wait(self, name: str, timeout: float = 600, from_agent: str = "") -> _Msg | None:
        try:
            return await asyncio.wait_for(self._queues[name].get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def mark_agent_done(self, name: str) -> None:
        pass

    def is_discussion_ended(self) -> bool:
        return self._discussion_ended

    def mark_discussion_ended(self) -> None:
        self._discussion_ended = True
        for evt in self._interrupt_events.values():
            evt.set()


class _StubConfig:
    max_tokens: int = 2000


class _StubEngine:
    def __init__(self) -> None:
        from nanobot.core.history import History
        self._running = True
        self._broadcast_tasks: dict[str, asyncio.Task] = {}
        self._runners: dict[str, Any] = {}
        self._request_log: list[dict] = []
        self.registry: dict[str, dict[str, Any]] = {"Kirk": {"model": "test/model"}}
        self.provider: Any = None
        self.config = _StubConfig()
        self.history = History()
        self._send_calls: list[str] = []
        self.stream_replies = False
        self._active_stream: Any = None
        self._debug_context: Any = None

    def _get_agent_tools(self, cfg: dict, reg: Any, agent_name: str = "") -> None:
        return None

    def get_agent_enabled_tool_names(self, name: str) -> list[str]:
        return []

    def _build_agent_prompt(self, *a: Any, **kw: Any) -> list[dict]:
        return [{"role": "user", "content": "test prompt"}]

    def _clean_response(self, content: str, name: str) -> str:
        return content

    def _persist_after_history_write(self, sender: str, content: str) -> None:
        pass

    async def _send(self, msg: str) -> None:
        self._send_calls.append(msg)

    async def _send_and_get_id_fn(self, text: str) -> int:
        self._send_calls.append(text)
        return 1

    async def _edit_fn(self, msg_id: int, text: str) -> None:
        pass

    def register_active_stream(self, stream: Any) -> None:
        self._active_stream = stream

    def unregister_active_stream(self, stream: Any) -> None:
        if self._active_stream is stream:
            self._active_stream = None


class _NoopTracker:
    async def set_state(self, name: str, state: str, **kw: Any) -> None:
        pass


class _NoopView:
    async def on_tool_start(self, *a: Any, **kw: Any) -> None:
        pass

    async def on_tool_result(self, *a: Any, **kw: Any) -> None:
        pass


class _NoopSearchPool:
    def on_output(self, name: str) -> None:
        pass

    def status(self) -> str:
        return ""


# ── Driver ──────────────────────────────────────────────────────────────────

async def _run_kirk(monkeypatch: pytest.MonkeyPatch, fake_loop: _ScriptedToolLoop,
                    engine: _StubEngine, mailbox: _StubMailbox) -> tuple:
    leader_end_event = asyncio.Event()

    def _end_side_effect(call_no: int) -> None:
        # Simulate EndDiscussionTool success: engine stops, events set.
        if fake_loop is not None and engine._running:
            engine._running = False
            leader_end_event.set()
            mailbox.mark_discussion_ended()

    env = AgentCycleEnv(
        engine=engine,
        mailbox=mailbox,
        leader_name="Kirk",
        leader_end_event=leader_end_event,
        agent_ranks={"Kirk": 2},
        ranks_map={"Kirk": "advanced"},
        agent_tool_registries={"Kirk": {}},
        agents=["Kirk", "Harper"],
        exec_agents=["Kirk", "Harper"],
        non_leader_agents=["Harper"],
        gc_settings={},
        pool=None,
        search_pool=_NoopSearchPool(),
        tracker=_NoopTracker(),
        view=_NoopView(),
        total=2,
        user_question="测试问题",
        trigger_realtime_interrupts=lambda **kw: asyncio.sleep(0),
        valid_agent_sampling=lambda cfg: {},
        base_sampling={},
    )
    monkeypatch.setattr(
        "nanobot.groupchat.runtime.tools.tool_loop.tool_loop", fake_loop
    )
    return await run_agent_cycle(env, "Kirk", 0), leader_end_event


def _mk(engine: _StubEngine, mailbox: _StubMailbox) -> None:
    mailbox.create("Kirk")
    mailbox.create("Harper")


# ── Scenarios ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_interrupt_after_end_exits_immediately(monkeypatch):
    """Incident replay: end_discussion success → guard-forced re-entry →
    next tool_loop call interrupted (mark_discussion_ended fires all events).
    Must exit at once, NOT re-enter (old behavior stalled 3 minutes)."""
    engine, mailbox = _StubEngine(), _StubMailbox()
    _mk(engine, mailbox)
    loop = _ScriptedToolLoop([
        _Result(finish_reason="end_discussion",
                tools_used=["exec", "memory_palace", "end_discussion"]),
        _Result(finish_reason="interrupted", content="部分结论草稿"),
    ])

    def _end_on_first(call_no: int) -> None:
        if call_no == 1:
            engine._running = False
            mailbox.mark_discussion_ended()

    loop._on_call = _end_on_first
    (name, content, tools, meta), _ = await _run_kirk(monkeypatch, loop, engine, mailbox)

    assert loop.call_count == 2, (
        f"expected exit after 2 tool_loop calls, got {loop.call_count} "
        "(old code re-entered and kept looping)"
    )
    assert content == "部分结论草稿"


@pytest.mark.asyncio
async def test_h1_forced_synthesis_bounded(monkeypatch):
    """H1 cap: leader ended discussion, then idles (no tools, no text).
    Forced-synthesis retries must stop at the cap (2), not loop forever."""
    engine, mailbox = _StubEngine(), _StubMailbox()
    _mk(engine, mailbox)
    loop = _ScriptedToolLoop([
        _Result(finish_reason="end_discussion", tools_used=["end_discussion"]),
        _Result(),  # idle cycle → H1 retry 1
        _Result(),  # idle cycle → H1 retry 2
        _Result(),  # idle cycle → cap exceeded → break (this call happens)
        _Result(content="迟到总结"),  # only reached without the cap
    ])

    def _end_on_first(call_no: int) -> None:
        if call_no == 1:
            engine._running = False
            mailbox.mark_discussion_ended()

    loop._on_call = _end_on_first
    (name, content, tools, meta), _ = await _run_kirk(monkeypatch, loop, engine, mailbox)

    assert loop.call_count == 4, (
        f"H1 must force at most 2 synthesis cycles then exit; got {loop.call_count} calls"
    )
    assert not content


@pytest.mark.asyncio
async def test_happy_path_end_with_text_unchanged(monkeypatch):
    """Regression: end_discussion + synthesis text in the same cycle →
    display and exit immediately (single tool_loop call)."""
    engine, mailbox = _StubEngine(), _StubMailbox()
    _mk(engine, mailbox)
    loop = _ScriptedToolLoop([
        _Result(finish_reason="end_discussion", tools_used=["end_discussion"],
                content="## 结论\n一切正常。"),
    ])

    def _end_on_first(call_no: int) -> None:
        if call_no == 1:
            engine._running = False
            mailbox.mark_discussion_ended()

    loop._on_call = _end_on_first
    (name, content, tools, meta), _ = await _run_kirk(monkeypatch, loop, engine, mailbox)

    assert loop.call_count == 1
    assert "一切正常" in content
    assert any("一切正常" in m for m in engine._send_calls)


@pytest.mark.asyncio
async def test_normal_interrupt_still_reenters(monkeypatch):
    """Regression: a mid-round interrupt while the discussion is NOT ended
    must keep the old re-entry behavior (fix 1 must not swallow it)."""
    engine, mailbox = _StubEngine(), _StubMailbox()
    _mk(engine, mailbox)
    # A teammate message is queued for the post-interrupt drain / auto-wait.
    mailbox.send("Harper", ["Kirk"], "队友的补充数据")

    loop = _ScriptedToolLoop([
        _Result(finish_reason="interrupted", content=""),
        _Result(content="整合后的完整回复"),
    ])

    def _stop_during_second(call_no: int) -> None:
        # Stop the engine during cycle 2 so the post-wait check exits.
        if call_no == 2:
            engine._running = False
            mailbox.mark_discussion_ended()

    loop._on_call = _stop_during_second
    (name, content, tools, meta), _ = await _run_kirk(monkeypatch, loop, engine, mailbox)

    assert loop.call_count == 2, "normal interrupt must re-enter tool_loop once"
    assert content == "整合后的完整回复"
    assert any("打断" in m for m in engine._send_calls), (
        "interrupt UI notice should still be shown for live interrupts"
    )
