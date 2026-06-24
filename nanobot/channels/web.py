"""Web channel — thin transport adapter; commands routed via runtime InboundDispatcher."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import websockets
from loguru import logger
from pydantic import Field
from websockets.server import WebSocketServerProtocol

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.commands_core import CoreCommandsMixin
from nanobot.channels.telegram.callbacks.edit import EditCallbackMixin
from nanobot.channels.telegram.commands.agents import AgentCommandsMixin
from nanobot.channels.telegram.commands.groups import GroupCommandsMixin
from nanobot.channels.telegram.commands.log import LogCommandsMixin
from nanobot.channels.telegram.commands.providers import ProviderCommandsMixin
from nanobot.channels.telegram.commands.settings import SettingsCommandsMixin
from nanobot.channels.web_shim import _FakeApp
from nanobot.config.schema import Base
from nanobot.runtime.chat_events import ChatEventBus, ChatEventSink, HubChatSink
from nanobot.runtime.dispatch import InboundDispatcher


class WebConfig(Base):
    """Web channel configuration."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 18791
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    token: str = ""
    serve_ws: bool = True


class WebChannel(
    CoreCommandsMixin,
    AgentCommandsMixin,
    ProviderCommandsMixin,
    SettingsCommandsMixin,
    GroupCommandsMixin,
    LogCommandsMixin,
    EditCallbackMixin,
    BaseChannel,
):
    """Browser chat — reuses Telegram GroupChat command handlers."""

    name = "web"
    display_name = "Web"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WebConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebConfig = config
        self._server: websockets.server.Serve | None = None
        self._clients: dict[str, set[WebSocketServerProtocol]] = {}
        self._groupchat_engine = None
        self._edit_state: dict[str, dict] = {}
        self._app = _FakeApp(self)
        self._dispatcher = InboundDispatcher()
        self._chat_events = ChatEventBus()

    def set_chat_hub(self, hub) -> None:
        """Wire in-process dashboard chat polling (replaces HTTP→WS bridge)."""
        self.add_chat_sink(HubChatSink(hub))

    def add_chat_sink(self, sink: ChatEventSink) -> None:
        """Subscribe a side-effect consumer to chat events."""
        self._chat_events.subscribe(sink)

    def set_groupchat_engine(self, engine) -> None:
        self._groupchat_engine = engine
        try:
            pm = self._load_pm()
            for _name, info in pm.get("providers", {}).items():
                delays = info.get("retryDelays")
                if delays and hasattr(engine, "provider"):
                    engine.provider._retry_delays = tuple(delays)
                    break
        except Exception:
            pass
        logger.info("Web: group chat engine set with {} agents", len(engine.registry))

    def _ensure_gc_send(self, chat_id: str) -> None:
        if self._groupchat_engine:
            self._groupchat_engine.set_tool_context("web", chat_id)

    async def _reply_text(self, chat_id: str, text: str) -> None:
        await self.bus.publish_outbound(OutboundMessage(
            channel=self.name,
            chat_id=chat_id,
            content=text,
        ))

    async def _gc_send(self, chat_id: str, text: str) -> None:
        await self._reply_text(chat_id, text)

    async def start(self) -> None:
        self._running = True
        if not self.config.serve_ws:
            logger.info("Web channel: WS disabled (dashboard HTTP chat only)")
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                pass
            return
        logger.info("Web channel listening on ws://{}:{}", self.config.host, self.config.port)

        async def handler(websocket: WebSocketServerProtocol) -> None:
            await self._on_connect(websocket)

        self._server = await websockets.serve(
            handler,
            self.config.host,
            self.config.port,
            ping_interval=30,
            ping_timeout=60,
        )
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()

    async def send(self, msg: OutboundMessage) -> None:
        payload = {
            "type": "message",
            "role": "agent",
            "content": msg.content,
            "agent": msg.metadata.get("agent", ""),
            "progress": bool(msg.metadata.get("_progress")),
            "tool_hint": bool(msg.metadata.get("_tool_hint")),
        }
        await self._chat_events.publish(msg.chat_id, payload)
        await self._broadcast(msg.chat_id, payload)

    async def _on_connect(self, websocket: WebSocketServerProtocol) -> None:
        parsed = urlparse(websocket.request.path or "/")
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0]
        chat_id = (qs.get("chat_id") or ["dashboard"])[0]
        sender = (qs.get("sender") or ["web-user"])[0]

        if self.config.token and token != self.config.token:
            await websocket.close(1008, "unauthorized")
            return
        if not self.is_allowed(sender):
            await websocket.close(1008, "forbidden")
            return

        self._register(chat_id, websocket)
        self._ensure_gc_send(chat_id)
        await self._emit(websocket, {
            "type": "connected",
            "chat_id": chat_id,
            "active_agents": list(getattr(self._groupchat_engine, "active_agents", []) or []),
        })

        try:
            async for raw in websocket:
                await self._on_client_message(chat_id, sender, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._unregister(chat_id, websocket)

    async def _on_client_message(self, chat_id: str, sender: str, raw: str | bytes) -> None:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        content = text
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                if data.get("type") == "ping":
                    await self._broadcast(chat_id, {"type": "pong"})
                    return
                content = str(data.get("content", "")).strip()
        except json.JSONDecodeError:
            content = text.strip()

        if not content:
            return

        await self._dispatch_inbound(chat_id, sender, content)

    async def _dispatch_inbound(
        self,
        chat_id: str,
        sender: str,
        content: str,
        *,
        emit_user: bool = True,
    ) -> None:
        """Runtime dispatcher → slash commands / edit-state / MessageBus."""
        if emit_user:
            await self._emit_chat(chat_id, {"type": "message", "role": "user", "content": content})
        handled = await self._dispatcher.handle(
            self, chat_id, sender, content, bus=self.bus,
        )
        if not handled and not self._groupchat_engine:
            await self._reply_text(chat_id, "⚠️ 群聊引擎未初始化")

    def _register(self, chat_id: str, ws: WebSocketServerProtocol) -> None:
        self._clients.setdefault(chat_id, set()).add(ws)

    def _unregister(self, chat_id: str, ws: WebSocketServerProtocol) -> None:
        peers = self._clients.get(chat_id)
        if not peers:
            return
        peers.discard(ws)
        if not peers:
            self._clients.pop(chat_id, None)

    async def _emit(self, ws: WebSocketServerProtocol, payload: dict) -> None:
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except websockets.ConnectionClosed:
            pass

    async def _broadcast(self, chat_id: str, payload: dict) -> None:
        for ws in list(self._clients.get(chat_id, ())):
            await self._emit(ws, payload)

    async def _emit_chat(self, chat_id: str, payload: dict) -> None:
        await self._chat_events.publish(chat_id, payload)
        await self._broadcast(chat_id, payload)
