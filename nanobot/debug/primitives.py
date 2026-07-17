"""Direct debug primitives over provider + collab runtime ports."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nanobot.groupchat.runtime.agent_runner import AgentRunner
from nanobot.groupchat.runtime.collab_bus import CollabBus, deliver
from nanobot.groupchat.runtime.cycle_controller import (
    CycleAction,
    CycleContext,
    CycleController,
    CycleDecision,
)
from nanobot.groupchat.runtime.mailbox import MailboxHub
from nanobot.groupchat.runtime.tools.tool_loop import ToolLoopResult, tool_loop
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.tools.registry import ToolRegistry


@dataclass
class CallResult:
    """Outcome of :func:`call_llm`."""

    content: str | None
    finish_reason: str
    attempts: int
    latency_s: float
    usage: dict[str, int] = field(default_factory=dict)
    status_code: int | None = None
    retry_log: list[dict[str, Any]] = field(default_factory=list)
    attempt_log: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    recovery_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    """Runtime / disk snapshot for debugging."""

    source: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "data": self.data}


def load_timeout_settings() -> dict[str, Any]:
    """Return effective groupchat timeout knobs (file + defaults)."""
    from nanobot.groupchat.runtime.broadcast_orchestrator import load_groupchat_settings

    return load_groupchat_settings()


def decide_error_recovery(
    *,
    finish_reason: str,
    timeout_recovery_count: int = 0,
    consecutive_error_count: int = 0,
    max_consecutive_errors: int = 3,
    agent_name: str = "debug",
    is_leader: bool = False,
    cycle: int = 1,
) -> CycleDecision:
    """Expose CycleController.decide_error_recovery for dry inspection."""
    ctrl = CycleController(agent_name)
    ctx = CycleContext(
        agent_name=agent_name,
        is_leader=is_leader,
        cycle=cycle,
        max_cycles=999,
        total_agents=1,
        engine_running=True,
        discussion_ended=False,
        leader_ended_discussion=False,
        leader_end_event_set=False,
        finish_reason=finish_reason,
        content="",
        tools_used=(),
        substantive_tools=frozenset(),
        timeout_recovery_count=timeout_recovery_count,
        consecutive_error_count=consecutive_error_count,
        max_consecutive_errors=max_consecutive_errors,
    )
    return ctrl.decide_error_recovery(ctx)


async def call_llm(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    call_timeout: float | None = None,
    max_attempts: int = 1,
    retry_on_timeout: bool = True,
    retry_on_transient_error: bool = True,
    metadata: dict[str, Any] | None = None,
    use_gc_timeout_default: bool = True,
) -> CallResult:
    """Call the model with per-attempt timeout and limited outer attempts.

    Mirrors production layering:
    - each attempt uses provider.chat_with_retry (inner transient retries)
    - each attempt is bounded by call_timeout (like tool_loop)
    - outer attempts re-issue when finish_reason is timeout (optional) or
      transient error (optional), consistent with timeout_recovery spirit
    - CycleController recovery action is recorded on the final result
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    if call_timeout is None and use_gc_timeout_default:
        call_timeout = float(load_timeout_settings().get("call_timeout", 90))

    attempt_log: list[dict[str, Any]] = []
    t_all = time.time()
    last: LLMResponse | None = None
    used_attempts = 0
    timeout_recovery_count = 0
    consecutive_error_count = 0

    for attempt in range(1, max_attempts + 1):
        used_attempts = attempt
        t0 = time.time()
        try:
            coro = provider.chat_with_retry(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=(
                    max_tokens
                    if max_tokens is not None
                    else provider.generation.max_tokens
                ),
                metadata=metadata or {"trace_name": "debug_call_llm"},
            )
            if call_timeout and call_timeout > 0:
                response = await asyncio.wait_for(coro, timeout=call_timeout)
            else:
                response = await coro
            finish = response.finish_reason or "stop"
        except asyncio.TimeoutError:
            response = LLMResponse(
                content=f"Error: call_timeout after {call_timeout}s",
                finish_reason="timeout",
            )
            finish = "timeout"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = LLMResponse(
                content=f"Error calling LLM: {exc}",
                finish_reason="error",
            )
            finish = "error"

        last = response
        latency = time.time() - t0
        decision = decide_error_recovery(
            finish_reason=finish,
            timeout_recovery_count=timeout_recovery_count,
            consecutive_error_count=consecutive_error_count,
        )
        attempt_log.append({
            "attempt": attempt,
            "finish_reason": finish,
            "latency_s": round(latency, 3),
            "status_code": response.status_code,
            "content_preview": (response.content or "")[:120],
            "provider_retry_log": list(response.retry_log or []),
            "recovery_action": decision.action.value,
        })

        if finish not in ("timeout", "error"):
            break

        if finish == "timeout":
            # Match agent_cycle: first timeout -> one recovery path; count resets
            # after a completed recovery attempt in production. Here each outer
            # attempt is an independent try with timeout_recovery_count tracking
            # whether we already used the first-retry slot in this call_llm.
            if decision.action is CycleAction.TIMEOUT_FIRST_RETRY and retry_on_timeout:
                timeout_recovery_count = 1
                if attempt < max_attempts:
                    continue
            break

        # error
        consecutive_error_count += 1
        if decision.action is CycleAction.ERROR_MAX_BREAK:
            break
        if not retry_on_transient_error:
            break
        if not provider._is_transient_error(response.content):
            break
        if attempt >= max_attempts:
            break

    assert last is not None
    final_decision = decide_error_recovery(
        finish_reason=last.finish_reason or "stop",
        timeout_recovery_count=timeout_recovery_count,
        consecutive_error_count=consecutive_error_count,
    )
    return CallResult(
        content=last.content,
        finish_reason=last.finish_reason or "stop",
        attempts=used_attempts,
        latency_s=round(time.time() - t_all, 3),
        usage=dict(last.usage or {}),
        status_code=last.status_code,
        retry_log=list(last.retry_log or []),
        attempt_log=attempt_log,
        model=model,
        recovery_action=final_decision.action.value,
    )


async def tool_loop_once(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_defs: list[dict[str, Any]] | None = None,
    max_tokens: int = 2048,
    max_iterations: int = 1,
    call_timeout: float | None = None,
    enable_tools: bool = True,
    is_leader: bool = False,
) -> ToolLoopResult:
    """Run a bounded tool_loop; default timeout from groupchat_settings."""
    if call_timeout is None:
        settings = load_timeout_settings()
        key = "leader_call_timeout" if is_leader else "call_timeout"
        call_timeout = float(settings.get(key, 90))

    reg = tool_registry or ToolRegistry()
    defs = tool_defs if enable_tools else None
    if enable_tools and defs is None and reg.tool_names:
        defs = reg.get_definitions()

    return await tool_loop(
        provider=provider,
        messages=list(messages),
        tool_registry=reg,
        model=model,
        max_tokens=max_tokens,
        max_iterations=max_iterations,
        tool_defs=defs,
        call_timeout=call_timeout,
        metadata={
            "trace_name": "debug_tool_loop",
            "log_agent": "debug",
            "log_mode": "debug",
        },
    )


async def collab_send(
    bus: CollabBus,
    sender: str,
    targets: list[str] | str,
    content: str,
) -> dict[str, Any]:
    """Deliver via CollabBus.deliver (same entry as agent_cycle)."""
    if isinstance(targets, str):
        targets = [targets]
    bus.create(sender)
    for t in targets:
        if t != "All":
            bus.create(t)
    n = deliver(bus, sender, targets, content)
    return {
        "ok": True,
        "sender": sender,
        "targets": targets,
        "delivered": n,
        "content_len": len(content),
    }


async def collab_wait(
    bus: CollabBus,
    agent: str,
    *,
    timeout: float = 30.0,
    from_agent: str | None = None,
) -> dict[str, Any]:
    """Wait on CollabBus for a message."""
    bus.create(agent)
    msg = await bus.wait(agent, timeout=float(timeout), from_agent=from_agent or "")
    if msg is None:
        return {"ok": False, "timeout": True, "agent": agent, "message": None}
    return {
        "ok": True,
        "timeout": False,
        "agent": agent,
        "message": {
            "sender": msg.sender,
            "content": msg.content,
            "targets": list(getattr(msg, "targets", []) or []),
            "id": getattr(msg, "id", None),
        },
    }


def collab_interrupt(
    runner: AgentRunner,
    *,
    sender: str = "用户",
    reason: str = "debug interrupt",
) -> dict[str, Any]:
    """Force cooperative interrupt via AgentRunner port."""
    ok = runner.force_interrupt(sender, reason)
    return {
        "ok": ok,
        "agent": runner.name,
        "state": runner.state,
        "interrupt_pending": runner.interrupt_pending,
        "reason": reason,
    }


def snapshot_disk(*, home: Path | None = None) -> Snapshot:
    """Read persisted groupchat state from ~/.nanobot."""
    base = home or (Path.home() / ".nanobot")
    data: dict[str, Any] = {"home": str(base)}

    def _read_json(name: str) -> Any:
        p = base / name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return {"_error": str(e)}

    data["active_agents"] = _read_json("active_agents.json")
    data["current_session"] = _read_json("current_session.json")
    data["groupchat_settings"] = _read_json("groupchat_settings.json")
    data["groups"] = _read_json("groups.json")
    data["effective_timeouts"] = load_timeout_settings()

    events_path = base / "logs" / "room_events.jsonl"
    tail: list[Any] = []
    if events_path.exists():
        try:
            lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-15:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    tail.append(json.loads(line))
                except Exception:
                    tail.append({"raw": line[:200]})
        except Exception as e:
            tail = [{"_error": str(e)}]
    data["room_events_tail"] = tail

    pid_path = base / "logs" / "gateway.pid"
    data["gateway_pid_file"] = (
        pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else None
    )

    return Snapshot(source="disk", data=data)


def snapshot_runtime(
    *,
    agents: list[str],
    mailbox: MailboxHub,
    runners: dict[str, AgentRunner],
    extra: dict[str, Any] | None = None,
) -> Snapshot:
    """In-process collab sandbox snapshot via AgentRunner + MailboxHub."""
    busy = set(mailbox.busy_agents()) if hasattr(mailbox, "busy_agents") else set()
    waiting = set(getattr(mailbox, "_waiting", set()) or [])
    agent_states = {}
    for name in agents:
        r = runners.get(name)
        agent_states[name] = {
            "state": r.state if r else "missing",
            "interrupt_pending": r.interrupt_pending if r else None,
            "is_waiting": r.is_waiting if r else None,
            "busy": name in busy,
            "waiting_set": name in waiting,
        }
    data: dict[str, Any] = {
        "agents": list(agents),
        "discussion_ended": bool(
            mailbox.is_discussion_ended()
            if hasattr(mailbox, "is_discussion_ended")
            else False
        ),
        "round_log_len": len(getattr(mailbox, "round_log", []) or []),
        "agent_states": agent_states,
        "timeouts": load_timeout_settings(),
    }
    if extra:
        data["extra"] = extra
    return Snapshot(source="runtime", data=data)
