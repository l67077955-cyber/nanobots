"""Message models for inter-component communication."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InboundMessage:
    channel: str
    sender_id: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    session_key_override: str | None = None

    @property
    def session_key(self) -> str:
        if self.session_key_override:
            return self.session_key_override
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    channel: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
