"""Minimal Update/Message shims so web chat reuses Telegram command mixins."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


def _keyboard_lines(reply_markup: Any) -> list[str]:
    lines: list[str] = []
    rows = getattr(reply_markup, "inline_keyboard", None) or []
    for row in rows:
        for btn in row:
            label = getattr(btn, "text", None) or str(btn)
            data = getattr(btn, "callback_data", "")
            if data:
                lines.append(f"  • {label} ({data})")
            else:
                lines.append(f"  • {label}")
    return lines


@dataclass
class WebReplyMessage:
    """Fake telegram Message with reply_text routed to the web channel."""

    chat_id: str
    text: str
    channel: Any
    message_id: int = 0
    message_thread_id: int | None = None
    chat: Any = field(default_factory=lambda: SimpleNamespace(type="private"))

    async def reply_text(self, text: str, reply_markup: Any = None, **kwargs: Any) -> None:
        body = text or ""
        if reply_markup is not None:
            extras = _keyboard_lines(reply_markup)
            if extras:
                body = f"{body}\n" + "\n".join(extras) if body else "\n".join(extras)
        await self.channel._reply_text(self.chat_id, body)


@dataclass
class WebUser:
    id: str
    username: str | None = None
    first_name: str = "Web"


@dataclass
class WebUpdate:
    message: WebReplyMessage
    effective_user: WebUser


@dataclass
class WebContext:
    args: list[str] = field(default_factory=list)


class _FakeBot:
    def __init__(self, channel: Any) -> None:
        self._channel = channel

    async def send_message(self, chat_id: int | str, text: str, **kwargs: Any) -> None:
        body = text or ""
        markup = kwargs.get("reply_markup")
        if markup is not None:
            extras = _keyboard_lines(markup)
            if extras:
                body = f"{body}\n" + "\n".join(extras) if body else "\n".join(extras)
        await self._channel._reply_text(str(chat_id), body)


class _FakeApp:
    def __init__(self, channel: Any) -> None:
        self.bot = _FakeBot(channel)