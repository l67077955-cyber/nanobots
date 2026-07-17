"""CLI: ``nanobot debug ...`` — exercise debug primitives against current runtime."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import typer
from rich.console import Console

from nanobot.debug.primitives import (
    call_llm,
    collab_interrupt,
    collab_send,
    collab_wait,
    decide_error_recovery,
    load_timeout_settings,
    snapshot_disk,
    snapshot_runtime,
    tool_loop_once,
)
from nanobot.debug.session import DebugSession, load_active_agents_from_disk

debug_app = typer.Typer(
    name="debug",
    help="Debug primitives over current runtime (default dry-run; use --live for API).",
    no_args_is_help=True,
)
console = Console()


def _run(coro):
    return asyncio.run(coro)


def _print_json(obj: Any) -> None:
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    elif hasattr(obj, "action"):
        obj = {"action": obj.action.value if hasattr(obj.action, "value") else str(obj.action)}
    console.print_json(json.dumps(obj, ensure_ascii=False, default=str))


def _parse_agents(agents: Optional[str]) -> list[str] | None:
    if not agents:
        return None
    return [a.strip() for a in agents.split(",") if a.strip()]


@debug_app.command("settings")
def debug_settings():
    """Show effective groupchat timeout settings (defaults + file)."""
    _print_json(load_timeout_settings())


@debug_app.command("recovery")
def debug_recovery(
    finish: str = typer.Option("timeout", "--finish", help="finish_reason: timeout|error|stop"),
    timeout_count: int = typer.Option(0, "--timeout-count"),
    error_count: int = typer.Option(0, "--error-count"),
    max_errors: int = typer.Option(3, "--max-errors"),
    leader: bool = typer.Option(False, "--leader"),
):
    """Inspect CycleController.decide_error_recovery for a synthetic state."""
    decision = decide_error_recovery(
        finish_reason=finish,
        timeout_recovery_count=timeout_count,
        consecutive_error_count=error_count,
        max_consecutive_errors=max_errors,
        is_leader=leader,
    )
    _print_json({
        "finish_reason": finish,
        "timeout_recovery_count": timeout_count,
        "consecutive_error_count": error_count,
        "action": decision.action.value,
    })


@debug_app.command("call")
def debug_call(
    message: str = typer.Option(..., "--message", "-m", help="User message"),
    system: str = typer.Option("", "--system", help="Optional system prompt"),
    model: Optional[str] = typer.Option(None, "--model", help="Model id (live)"),
    live: bool = typer.Option(False, "--live", help="Use real provider from config"),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Per-attempt timeout (default: groupchat call_timeout)"
    ),
    attempts: int = typer.Option(1, "--attempts", help="Outer attempts on timeout/transient error"),
    max_tokens: int = typer.Option(512, "--max-tokens"),
):
    """Single LLM call with timeout / outer attempts."""

    async def _main():
        session = DebugSession.create(live=live, model=model)
        for note in session.notes:
            console.print(f"[dim]{note}[/dim]")
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": message})
        result = await call_llm(
            session.provider,
            messages,
            model=session.model,
            call_timeout=timeout,
            max_attempts=attempts,
            max_tokens=max_tokens,
        )
        _print_json(result)

    _run(_main())


@debug_app.command("tool-loop")
def debug_tool_loop(
    message: str = typer.Option(..., "--message", "-m"),
    model: Optional[str] = typer.Option(None, "--model"),
    live: bool = typer.Option(False, "--live"),
    timeout: Optional[float] = typer.Option(
        None, "--timeout", help="Default: call_timeout or leader_call_timeout"
    ),
    iterations: int = typer.Option(1, "--iterations", help="Max tool_loop iterations"),
    no_tools: bool = typer.Option(False, "--no-tools", help="Disable tool definitions"),
    leader: bool = typer.Option(False, "--leader", help="Use leader_call_timeout default"),
    max_tokens: int = typer.Option(1024, "--max-tokens"),
):
    """Run a bounded tool_loop (production call_timeout defaults)."""

    async def _main():
        session = DebugSession.create(live=live, model=model)
        for note in session.notes:
            console.print(f"[dim]{note}[/dim]")
        messages = [{"role": "user", "content": message}]
        result = await tool_loop_once(
            session.provider,
            messages,
            model=session.model,
            max_iterations=iterations,
            call_timeout=timeout,
            enable_tools=not no_tools,
            max_tokens=max_tokens,
            is_leader=leader,
        )
        _print_json({
            "content": result.content,
            "finish_reason": result.finish_reason,
            "tools_used": result.tools_used,
            "iterations": result.iterations,
            "latency": round(result.latency, 3),
            "token_usage": result.token_usage,
            "status_code": result.status_code,
            "tool_calls_detail": result.tool_calls_detail,
        })

    _run(_main())


@debug_app.command("send")
def debug_send(
    message: str = typer.Option(..., "--message", "-m"),
    sender: str = typer.Option("A", "--from", help="Sender agent name"),
    to: str = typer.Option("B", "--to", help="Target agent or All"),
    agents: Optional[str] = typer.Option(None, "--agents", help="Comma-separated sandbox agents"),
):
    """CollabBus send in isolated sandbox (not live gateway)."""

    async def _main():
        names = _parse_agents(agents) or list(({sender, to} - {"All"}) | {"A", "B"})
        if sender not in names:
            names.append(sender)
        if to != "All" and to not in names:
            names.append(to)
        session = DebugSession.create(live=False, agents=names)
        out = await collab_send(session.bus, sender, [to], message)
        _print_json(out)

    _run(_main())


@debug_app.command("wait")
def debug_wait(
    agent: str = typer.Option("B", "--agent", "-a"),
    timeout: float = typer.Option(5.0, "--timeout"),
    from_agent: Optional[str] = typer.Option(None, "--from"),
    pre_send: Optional[str] = typer.Option(
        None, "--pre-send", help="Optional message to send from A before wait"
    ),
    agents: Optional[str] = typer.Option(None, "--agents"),
):
    """CollabBus wait in isolated sandbox; optional --pre-send from A."""

    async def _main():
        names = _parse_agents(agents) or ["A", "B", agent]
        if agent not in names:
            names.append(agent)
        session = DebugSession.create(live=False, agents=list(dict.fromkeys(names)))
        if pre_send is not None:
            await collab_send(session.bus, "A", [agent], pre_send)
        out = await collab_wait(
            session.bus, agent, timeout=timeout, from_agent=from_agent
        )
        _print_json(out)

    _run(_main())


@debug_app.command("interrupt")
def debug_interrupt(
    agent: str = typer.Option("A", "--agent", "-a"),
    reason: str = typer.Option("debug interrupt", "--reason"),
    agents: Optional[str] = typer.Option(None, "--agents"),
):
    """Force interrupt via AgentRunner in isolated sandbox."""
    names = _parse_agents(agents) or [agent, "B"]
    if agent not in names:
        names.append(agent)
    session = DebugSession.create(live=False, agents=list(dict.fromkeys(names)))
    out = collab_interrupt(session.runner(agent), reason=reason)
    _print_json(out)


@debug_app.command("snapshot")
def debug_snapshot(
    runtime: bool = typer.Option(
        False, "--runtime", help="Also build an isolated runtime sandbox snapshot"
    ),
    agents: Optional[str] = typer.Option(None, "--agents"),
):
    """Show disk state of live install; optional isolated runtime snapshot."""
    disk = snapshot_disk()
    console.print("[bold]disk[/bold]")
    _print_json(disk)

    active = load_active_agents_from_disk()
    if active:
        console.print(f"[dim]active_agents.json → {active}[/dim]")

    if runtime:
        names = _parse_agents(agents) or active or ["A", "B"]
        session = DebugSession.create(live=False, agents=names)
        snap = snapshot_runtime(
            agents=session.agents,
            mailbox=session.mailbox,
            runners=session.runners,
            extra={"notes": session.notes},
        )
        console.print("[bold]runtime (sandbox)[/bold]")
        _print_json(snap)


@debug_app.command("collab")
def debug_collab(
    message: str = typer.Option("ping", "--message", "-m"),
    timeout: float = typer.Option(3.0, "--timeout"),
):
    """One-shot sandbox: deliver A→B, wait, interrupt A (CollabBus + AgentRunner)."""

    async def _main():
        session = DebugSession.create(live=False, agents=["A", "B"])
        send_out = await collab_send(session.bus, "A", ["B"], message)
        wait_out = await collab_wait(session.bus, "B", timeout=timeout)
        intr_out = collab_interrupt(session.runner("A"), reason="collab demo")
        snap = snapshot_runtime(
            agents=session.agents,
            mailbox=session.mailbox,
            runners=session.runners,
        )
        _print_json({
            "send": send_out,
            "wait": wait_out,
            "interrupt": intr_out,
            "snapshot": snap.to_dict(),
        })

    _run(_main())
