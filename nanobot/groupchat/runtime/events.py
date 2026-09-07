"""Event bus for the groupchat orchestration.

Activates the previously-unused BroadcastEventDispatcher scaffolding into the
single seam where mods (see ``nanobot/mods/``) observe — and later filter —
orchestration behaviour. Core orchestration emits domain events at a small
set of converged chokepoints (UserIngress, RoundLifecycle, broadcast cycle
boundaries, MailboxHub.send, tool results); anything else stays internal.

Design rules, load-bearing for the mod architecture:
- **Zero-listener cost**: emitting with no subscribers is a dict lookup.
  With mods disabled by default, orchestration behaviour is bit-identical
  to pre-bus code (pinned by parity tests).
- **Per-listener isolation**: one failing listener never breaks the emitter
  or other listeners.
- **Sync and async emitters**: ``await bus.emit(...)`` for async call sites
  (sequential, ordered); ``bus.emit_nowait(...)`` for sync call sites such as
  RoundLifecycle transitions (schedules each listener as a task; unordered).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger

# ── Event catalogue ────────────────────────────────────────────────────────
# The stable surface mods may subscribe to. Payload fields documented per
# event; treat adding a field as compatible, removing/renaming as breaking.

EVENTS: dict[str, str] = {
    # user message flow (emitted from UserIngress)
    "user:round_opened":    "engine, user_input, agent_count",
    "user:message_delivered": "engine, message, delivered_to, interrupted",
    "user:message_requeued": "engine, message",
    "summary:deferred":     "engine",
    # round lifecycle (emitted from RoundLifecycle / broadcast_round)
    "round:started":        "engine, agents, leader, round_num",
    "round:winding_down":   "engine, reason, leader_exempt",
    "round:reopened":       "engine, reason",
    "round:ended":          "engine",
    # agent cycle boundaries (emitted from broadcast._run_one)
    "agent:cycle_output":   "engine, agent, chars, tools",
    "agent:interrupted":    "engine, agent, by",
    "agent:waiting":        "engine, agent",
    "agent:reactivated":    "engine, agent, message, recent_texts, inject (tier-2 mutable list)",
    "agent:done":           "engine, agent, reason",
    # inter-agent delivery (emitted from MailboxHub.send)
    "message:delivered":    "sender, targets, delivered, preview",
    # tool results (emitted from tool_loop)
    "tool:result":          "tool, ok, chars",
}


class BroadcastEventDispatcher:
    """Async pub/sub with per-listener exception isolation."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[..., Awaitable[None]]]] = {}

    # ── Subscription ───────────────────────────────────────────────────────

    def on(self, event_name: str, callback: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        """Subscribe *callback* to *event_name*. Returns the callback (pass
        the same callable to :meth:`off` to unsubscribe)."""
        if event_name not in self._listeners:
            self._listeners[event_name] = []
        self._listeners[event_name].append(callback)
        return callback

    def off(self, event_name: str, callback: Callable[..., Awaitable[None]]) -> None:
        """Unsubscribe; unknown callbacks are ignored."""
        listeners = self._listeners.get(event_name)
        if not listeners:
            return
        try:
            listeners.remove(callback)
        except ValueError:
            pass

    def listener_count(self, event_name: str) -> int:
        return len(self._listeners.get(event_name, ()))

    # ── Emission ───────────────────────────────────────────────────────────

    async def emit(self, event_name: str, **kwargs: Any) -> None:
        """Await every listener sequentially; failures are logged, not raised."""
        for cb in self._listeners.get(event_name, ()):
            try:
                await cb(**kwargs)
            except Exception as e:  # noqa: BLE001 — deliberate containment
                logger.error("events: listener {} failed for {}: {}", cb, event_name, e)

    def emit_nowait(self, event_name: str, **kwargs: Any) -> None:
        """Fire-and-forget variant for synchronous emitters.

        Schedules each listener as its own task when a loop is running; with
        no running loop (bare/test synchronous contexts) the event is dropped
        with a debug log — core behaviour must never depend on listeners.
        """
        listeners = self._listeners.get(event_name, ())
        if not listeners:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("events: no running loop, dropping {}:{}", event_name, kwargs)
            return
        for cb in listeners:
            async def _run(cb=cb, event_name=event_name, kwargs=kwargs) -> None:
                try:
                    await cb(**kwargs)
                except Exception as e:  # noqa: BLE001
                    logger.error("events: listener {} failed for {}: {}", cb, event_name, e)
            loop.create_task(_run())


# ── Default bus ────────────────────────────────────────────────────────────
# Module-level singleton so emitters that were never wired with a bus
# instance (and lazy import chains) all resolve to one bus per process.

_default_bus: BroadcastEventDispatcher | None = None


def get_bus() -> BroadcastEventDispatcher:
    global _default_bus
    if _default_bus is None:
        _default_bus = BroadcastEventDispatcher()
    return _default_bus


def set_bus(bus: BroadcastEventDispatcher | None) -> None:
    """Swap/reset the default bus (tests)."""
    global _default_bus
    _default_bus = bus
