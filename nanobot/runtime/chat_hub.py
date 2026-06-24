"""In-process chat event hub — dashboard polls this instead of a WS bridge."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable

DispatchFn = Callable[..., Awaitable[None]]


class ChatHub:
    """Thread-safe chat event buffer wired to WebChannel inbound dispatch."""

    def __init__(self, *, chat_id: str = "dashboard") -> None:
        self.chat_id = chat_id
        self._events: deque[dict[str, Any]] = deque(maxlen=300)
        self._seq = 0
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dispatch: DispatchFn | None = None
        self.connected = False
        self.last_error = ""

    def attach(self, loop: asyncio.AbstractEventLoop, dispatch: DispatchFn) -> None:
        self._loop = loop
        self._dispatch = dispatch
        self.connected = True
        self.last_error = ""
        self.push({"type": "system", "content": "已连接 nanobot 群聊引擎", "role": "system"})

    def detach(self) -> None:
        self.connected = False
        self._loop = None
        self._dispatch = None

    def push(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            item = dict(payload)
            item["id"] = self._seq
            item["ts"] = time.time()
            self._events.append(item)

    def send(self, content: str, *, echo: bool = True) -> bool:
        if not self._loop or not self._dispatch or not self.connected:
            self.last_error = "chat runtime not ready"
            return False
        text = (content or "").strip()
        if not text:
            self.last_error = "empty message"
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._dispatch(self.chat_id, "web-user", text, emit_user=echo),
                self._loop,
            )
            fut.add_done_callback(self._record_dispatch_error)
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def _record_dispatch_error(self, fut: asyncio.Future) -> None:
        try:
            fut.result()
        except Exception as exc:
            self.last_error = str(exc)

    def events_after(self, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if int(e.get("id", 0)) > after_id]

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_error": self.last_error,
            "event_count": len(self._events),
            "latest_id": self._seq,
            "mode": "gateway",
        }
