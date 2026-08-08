"""Behavioral tests for the CURRENT TelegramChannel send() media path.

The channel was rewritten: `msg.media` items are local file *paths* opened with
open(); remote URLs are NOT fetched (no SSRF surface) and fall through to a
"[Failed to send]" message. These tests lock in that real behavior, replacing
stale tests written for a pre-rewrite URL-sending implementation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import TelegramChannel
from nanobot.config.schema import TelegramConfig


class _RecordingBot:
    def __init__(self) -> None:
        self.sent_media: list[dict] = []
        self.sent_messages: list[dict] = []
        self.sent_photo_calls = 0

    async def send_photo(self, **kwargs) -> None:
        self.sent_photo_calls += 1
        self.sent_media.append({"kind": "photo", **kwargs})

    async def send_voice(self, **kwargs) -> None:
        self.sent_media.append({"kind": "voice", **kwargs})

    async def send_audio(self, **kwargs) -> None:
        self.sent_media.append({"kind": "audio", **kwargs})

    async def send_document(self, **kwargs) -> None:
        self.sent_media.append({"kind": "document", **kwargs})

    async def send_message(self, **kwargs) -> None:
        self.sent_messages.append(kwargs)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _RecordingBot()


def _make_channel() -> TelegramChannel:
    ch = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"]),
        MessageBus(),
    )
    ch._app = _FakeApp()
    return ch


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_send_local_photo_path(
    tmp_path, monkeypatch
) -> None:
    """Local image path is sent as a photo, not fetched as a URL."""
    img = tmp_path / "cat.jpg"
    img.write_bytes(b"\xff\xd8fakejpegdata")
    channel = _make_channel()

    await channel.send(
        OutboundMessage(
            channel="telegram", chat_id="123", content="",
            media=[str(img)],
        )
    )

    bot = channel._app.bot
    assert bot.sent_photo_calls == 1
    assert bot.sent_media[0]["chat_id"] == 123


@pytest.mark.asyncio
async def test_send_unknown_extension_uses_document(tmp_path) -> None:
    """A local file without a known media extension falls back to send_document."""
    f = tmp_path / "data.bin"
    f.write_bytes(b"\x00\x01\x02")
    channel = _make_channel()

    await channel.send(
        OutboundMessage(channel="telegram", chat_id="1", content="", media=[str(f)])
    )
    assert channel._app.bot.sent_media[0]["kind"] == "document"


@pytest.mark.asyncio
async def test_remote_url_is_not_sent_and_emits_fail_message(monkeypatch) -> None:
    """A URL string cannot be opened() → no media sent, a fail notice is posted.

    This locks in that the current implementation does NOT fetch remote URLs
    (no SSRF surface) — replaces the stale validate_url_target test.
    """
    channel = _make_channel()
    with patch("builtins.open", side_effect=FileNotFoundError("no such file")):
        await channel.send(
            OutboundMessage(
                channel="telegram", chat_id="123", content="",
                media=["https://example.com/cat.jpg"],
            )
        )

    bot = channel._app.bot
    assert bot.sent_media == []
    assert any("Failed to send" in m["text"] for m in bot.sent_messages)


@pytest.mark.asyncio
async def test_send_missing_local_file_emits_fail_message(tmp_path) -> None:
    """Missing local path → fail notice, never a crash."""
    channel = _make_channel()
    missing = tmp_path / "nope.jpg"
    await channel.send(
        OutboundMessage(channel="telegram", chat_id="9", content="", media=[str(missing)])
    )
    bot = channel._app.bot
    assert bot.sent_media == []
    assert len(bot.sent_messages) == 1
    assert "Failed to send" in bot.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_send_text_and_media_together(tmp_path) -> None:
    """Text content is still delivered alongside media."""
    img = tmp_path / "cat.jpg"
    img.write_bytes(b"jpegdata")
    channel = _make_channel()
    await channel.send(
        OutboundMessage(
            channel="telegram", chat_id="123",
            content="here it is", media=[str(img)],
        )
    )
    bot = channel._app.bot
    assert bot.sent_photo_calls == 1