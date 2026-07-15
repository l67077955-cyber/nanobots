"""Orchestra events and cross-layer hooks for group chat broadcast."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.runtime.mailbox import MailboxHub


class BroadcastEventDispatcher:
    def __init__(self):
        self._listeners: dict[str, list[Callable[..., Awaitable[None]]]] = {}

    def on(self, event_name: str, callback: Callable[..., Awaitable[None]]) -> None:
        if event_name not in self._listeners:
            self._listeners[event_name] = []
        self._listeners[event_name].append(callback)

    async def emit(self, event_name: str, **kwargs: Any) -> None:
        listeners = self._listeners.get(event_name, [])
        for cb in listeners:
            try:
                await cb(**kwargs)
            except Exception as e:
                logger.error(f"Error in event listener for {event_name}: {e}")


async def trigger_realtime_interrupts(
    sender: str,
    targets: list[str],
    mailbox: MailboxHub,
    engine: Any,
    leader_name: str | None,
) -> None:
    """Handle bi-directional real-time interrupts after a successful chatroom_send."""
    _targets_lower = [t.lower() for t in targets]

    has_others = "all" in _targets_lower or any(t != sender.lower() for t in _targets_lower)
    if not has_others:
        return

    _interrupted_count = 0
    for _tgt in targets:
        if _tgt.lower() == "all":
            _interrupted_count += mailbox.interrupt_busy_agents(sender)
            break
        if mailbox._try_interrupt(_tgt, sender):
            _interrupted_count += 1

    if _interrupted_count > 0:
        _dir = "队友" if sender != leader_name else "Leader"
        _recv_str = ", ".join(targets)
        logger.info(
            "Broadcast: {} {} → {} 实时打断 {} 个 busy agent",
            _dir, sender, _recv_str, _interrupted_count,
        )