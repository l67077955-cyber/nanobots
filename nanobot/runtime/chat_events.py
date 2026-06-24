"""Chat event fanout for dashboard chat surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus


@dataclass(frozen=True)
class ChatEvent:
    """A chat event emitted by an interactive surface."""

    chat_id: str
    payload: dict[str, Any]


class ChatEventSink(Protocol):
    """Observer interface for chat event consumers."""

    async def publish(self, event: ChatEvent) -> None:
        """Consume a chat event."""


class ChatEventBus:
    """Small async observer bus used by channels to fan out chat events."""

    def __init__(self) -> None:
        self._sinks: list[ChatEventSink] = []

    def subscribe(self, sink: ChatEventSink) -> None:
        self._sinks.append(sink)

    async def publish(self, chat_id: str, payload: dict[str, Any]) -> None:
        if not self._sinks:
            return
        event = ChatEvent(chat_id=chat_id, payload=payload)
        for sink in list(self._sinks):
            await sink.publish(event)


class HubChatSink:
    """Writes web chat events into the dashboard polling hub."""

    def __init__(self, hub: Any) -> None:
        self._hub = hub

    async def publish(self, event: ChatEvent) -> None:
        if event.chat_id == self._hub.chat_id:
            self._hub.push(event.payload)


class OutboundMirrorSink:
    """Mirrors chat events to another outbound channel."""

    def __init__(self, bus: MessageBus, *, channel: str, chat_id: str) -> None:
        self._bus = bus
        self._channel = channel
        self._chat_id = chat_id

    async def publish(self, event: ChatEvent) -> None:
        payload = event.payload
        if payload.get("type") != "message":
            return
        content = str(payload.get("content", "")).strip()
        if not content:
            return
        role = payload.get("role")
        if role == "user":
            content = f"网页端: {content}"
        await self._bus.publish_outbound(OutboundMessage(
            channel=self._channel,
            chat_id=self._chat_id,
            content=content,
            metadata={
                "_mirror_from": "web",
                "_progress": bool(payload.get("progress")),
                "_tool_hint": bool(payload.get("tool_hint")),
            },
        ))
