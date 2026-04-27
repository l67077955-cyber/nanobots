"""Event dispatcher for decoupling the broadcast orchestrator from UI rendering."""
from typing import Any, Callable, Awaitable

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
                from loguru import logger
                logger.error(f"Error in event listener for {event_name}: {e}")
