"""Tests for nanobot.debug primitives (dry-run; server runtime aligned)."""

from __future__ import annotations

import asyncio

from nanobot.debug.primitives import (
    call_llm,
    collab_interrupt,
    collab_send,
    collab_wait,
    decide_error_recovery,
    load_timeout_settings,
    snapshot_runtime,
    tool_loop_once,
)
from nanobot.debug.session import DebugSession
from nanobot.debug.stub_provider import StubLLMProvider
from nanobot.groupchat.runtime.cycle_controller import CycleAction
from nanobot.providers.base import LLMResponse


def test_load_timeout_settings_has_keys():
    s = load_timeout_settings()
    assert "call_timeout" in s
    assert "leader_call_timeout" in s
    assert "global_timeout" in s


def test_decide_error_recovery_timeout_first():
    d = decide_error_recovery(finish_reason="timeout", timeout_recovery_count=0)
    assert d.action is CycleAction.TIMEOUT_FIRST_RETRY


def test_decide_error_recovery_error_max():
    d = decide_error_recovery(
        finish_reason="error",
        consecutive_error_count=3,
        max_consecutive_errors=3,
    )
    assert d.action is CycleAction.ERROR_MAX_BREAK


def test_call_llm_stub_ok():
    session = DebugSession.create(live=False)

    async def _run():
        return await call_llm(
            session.provider,
            [{"role": "user", "content": "hello"}],
            call_timeout=5,
            max_attempts=1,
            use_gc_timeout_default=False,
        )

    result = asyncio.run(_run())
    assert result.finish_reason == "stop"
    assert result.attempts == 1
    assert result.content and "hello" in result.content
    assert result.recovery_action is not None


def test_call_llm_outer_timeout_then_retry():
    slow = StubLLMProvider(latency_s=0.3)

    async def _run():
        return await call_llm(
            slow,
            [{"role": "user", "content": "x"}],
            call_timeout=0.05,
            max_attempts=2,
            retry_on_timeout=True,
            use_gc_timeout_default=False,
        )

    result = asyncio.run(_run())
    assert result.finish_reason == "timeout"
    assert result.attempts == 2
    assert len(result.attempt_log) == 2


def test_call_llm_transient_error_retries():
    provider = StubLLMProvider(fail_transient_times=1)
    provider._retry_delays = ()

    async def _run():
        return await call_llm(
            provider,
            [{"role": "user", "content": "hi"}],
            call_timeout=5,
            max_attempts=2,
            retry_on_transient_error=True,
            use_gc_timeout_default=False,
        )

    result = asyncio.run(_run())
    assert result.finish_reason == "stop"
    assert result.attempts == 2


def test_tool_loop_once_no_tools():
    session = DebugSession.create(live=False)

    async def _run():
        return await tool_loop_once(
            session.provider,
            [{"role": "user", "content": "ping"}],
            max_iterations=1,
            call_timeout=5,
            enable_tools=False,
        )

    result = asyncio.run(_run())
    assert result.finish_reason in ("stop", "error")
    assert result.iterations >= 1


def test_collab_send_wait_interrupt_via_bus():
    session = DebugSession.create(live=False, agents=["A", "B"])

    async def _run():
        await collab_send(session.bus, "A", ["B"], "hello-b")
        waited = await collab_wait(session.bus, "B", timeout=2)
        interrupted = collab_interrupt(session.runner("A"))
        snap = snapshot_runtime(
            agents=session.agents,
            mailbox=session.mailbox,
            runners=session.runners,
        )
        return waited, interrupted, snap

    waited, interrupted, snap = asyncio.run(_run())
    assert waited["ok"] is True
    assert waited["message"]["content"] == "hello-b"
    assert interrupted["ok"] is True
    assert snap.data["agents"] == ["A", "B"]
    assert "timeouts" in snap.data
