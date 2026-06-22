"""DEPRECATED: use gateway-integrated ChatHub (nanobot/runtime/chat_hub.py)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlencode

import websockets


class ChatBridge:
    """One persistent upstream WS; thread-safe send + event deque for HTTP polling."""

    def __init__(
        self,
        *,
        upstream_host: str = "127.0.0.1",
        upstream_port: int = 18791,
        upstream_token: str = "",
        chat_id: str = "dashboard",
    ) -> None:
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.upstream_token = upstream_token
        self.chat_id = chat_id
        self._events: deque[dict[str, Any]] = deque(maxlen=300)
        self._seq = 0
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._outbound: asyncio.Queue[str] | None = None
        self.connected = False
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="code-watch-chat-bridge", daemon=True)
        self._thread.start()

    def _upstream_url(self) -> str:
        qs = {"chat_id": self.chat_id, "sender": "web-user"}
        if self.upstream_token:
            qs["token"] = self.upstream_token
        return f"ws://{self.upstream_host}:{self.upstream_port}/?{urlencode(qs)}"

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._outbound = asyncio.Queue()
        self._loop.run_until_complete(self._maintain())

    async def _maintain(self) -> None:
        assert self._outbound is not None
        while True:
            try:
                async with websockets.connect(
                    self._upstream_url(),
                    ping_interval=30,
                    ping_timeout=60,
                ) as ws:
                    self.connected = True
                    self.last_error = ""
                    self._push({"type": "system", "content": "已连接 nanobot 群聊引擎", "role": "system"})

                    async def reader() -> None:
                        async for raw in ws:
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                payload = {"type": "message", "role": "agent", "content": str(raw)}
                            if isinstance(payload, dict):
                                self._push(payload)

                    async def writer() -> None:
                        while True:
                            body = await self._outbound.get()
                            await ws.send(body)

                    await asyncio.gather(reader(), writer())
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                self._push({"type": "error", "content": f"连接断开: {exc}", "role": "system"})
                await asyncio.sleep(2)

    def _push(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            item = dict(payload)
            item["id"] = self._seq
            item["ts"] = time.time()
            self._events.append(item)

    def send(self, content: str) -> bool:
        if not self._loop or not self._outbound or not self.connected:
            return False
        body = json.dumps({"type": "chat", "content": content}, ensure_ascii=False)
        try:
            fut = asyncio.run_coroutine_threadsafe(self._outbound.put(body), self._loop)
            fut.result(timeout=3)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def events_after(self, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if int(e.get("id", 0)) > after_id]

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_error": self.last_error,
            "event_count": len(self._events),
            "latest_id": self._seq,
        }