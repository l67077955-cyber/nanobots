from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import TelegramChannel, TelegramConfig


# ---------------------------------------------------------------------------
# Test doubles. Helpers used only by the removed pool/start tests
# (_FakeHTTPXRequest, _FakeBuilder) are retained so the P3 connection-pool
# tests and P6 message-path tests can reuse them when those areas are
# rebuilt.
# ---------------------------------------------------------------------------
class _FakeHTTPXRequest:
    instances: list["_FakeHTTPXRequest"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    @classmethod
    def clear(cls) -> None:
        cls.instances.clear()


class _FakeUpdater:
    def __init__(self, on_start_polling) -> None:
        self._on_start_polling = on_start_polling

    async def start_polling(self, **kwargs) -> None:
        self._on_start_polling()


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.sent_media: list[dict] = []
        self.get_me_calls = 0

    async def get_me(self):
        self.get_me_calls += 1
        return SimpleNamespace(id=999, username="nanobot_test")

    async def set_my_commands(self, commands) -> None:
        self.commands = commands

    async def send_message(self, **kwargs) -> None:
        self.sent_messages.append(kwargs)

    async def send_photo(self, **kwargs) -> None:
        self.sent_media.append({"kind": "photo", **kwargs})

    async def send_voice(self, **kwargs) -> None:
        self.sent_media.append({"kind": "voice", **kwargs})

    async def send_audio(self, **kwargs) -> None:
        self.sent_media.append({"kind": "audio", **kwargs})

    async def send_document(self, **kwargs) -> None:
        self.sent_media.append({"kind": "document", **kwargs})

    async def send_chat_action(self, **kwargs) -> None:
        pass

    async def get_file(self, file_id: str):
        """Return a fake file that 'downloads' to a path (for reply-to-media tests)."""
        async def _fake_download(path) -> None:
            pass
        return SimpleNamespace(download_to_drive=_fake_download)


class _FakeApp:
    def __init__(self, on_start_polling) -> None:
        self.bot = _FakeBot()
        self.updater = _FakeUpdater(on_start_polling)
        self.handlers = []
        self.error_handlers = []

    def add_error_handler(self, handler) -> None:
        self.error_handlers.append(handler)

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        pass

    async def start(self) -> None:
        pass


class _FakeBuilder:
    def __init__(self, app: _FakeApp) -> None:
        self.app = app
        self.token_value = None
        self.request_value = None
        self.get_updates_request_value = None

    def token(self, token: str):
        self.token_value = token
        return self

    def request(self, request):
        self.request_value = request
        return self

    def get_updates_request(self, request):
        self.get_updates_request_value = request
        return self

    def proxy(self, _proxy):
        raise AssertionError("builder.proxy should not be called when request is set")

    def get_updates_proxy(self, _proxy):
        raise AssertionError("builder.get_updates_proxy should not be called when request is set")

    def build(self):
        return self.app


def _make_telegram_update(
    *,
    chat_type: str = "group",
    text: str | None = None,
    caption: str | None = None,
    entities=None,
    caption_entities=None,
    reply_to_message=None,
):
    user = SimpleNamespace(id=12345, username="alice", first_name="Alice")
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, is_forum=False),
        chat_id=-100123,
        text=text,
        caption=caption,
        entities=entities or [],
        caption_entities=caption_entities or [],
        reply_to_message=reply_to_message,
        photo=None,
        voice=None,
        audio=None,
        document=None,
        media_group_id=None,
        message_thread_id=None,
        message_id=1,
    )
    return SimpleNamespace(message=message, effective_user=user)


def test_derive_topic_session_key_uses_thread_id() -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(type="supergroup"),
        chat_id=-100123,
        message_thread_id=42,
    )

    assert TelegramChannel._derive_topic_session_key(message) == "telegram:-100123:topic:42"


def test_get_extension_falls_back_to_original_filename() -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())

    assert channel._get_extension("file", None, "report.pdf") == ".pdf"
    assert channel._get_extension("file", None, "archive.tar.gz") == ".tar.gz"


def test_telegram_group_policy_defaults_to_restricted() -> None:
    assert TelegramConfig().group_policy == "restricted"


def test_is_allowed_accepts_legacy_telegram_id_username_formats() -> None:
    channel = TelegramChannel(TelegramConfig(allow_from=["12345", "alice", "67890|bob"]), MessageBus())

    assert channel.is_allowed("12345|carol") is True
    assert channel.is_allowed("99999|alice") is True
    assert channel.is_allowed("67890|bob") is True


def test_is_allowed_rejects_invalid_legacy_telegram_sender_shapes() -> None:
    channel = TelegramChannel(TelegramConfig(allow_from=["alice"]), MessageBus())

    assert channel.is_allowed("attacker|alice|extra") is False
    assert channel.is_allowed("not-a-number|alice") is False


@pytest.mark.asyncio
async def test_send_progress_keeps_message_in_topic() -> None:
    config = TelegramConfig(enabled=True, token="123:abc", allow_from=["*"])
    channel = TelegramChannel(config, MessageBus())
    channel._app = _FakeApp(lambda: None)

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="hello",
            metadata={"_progress": True, "message_thread_id": 42},
        )
    )

    assert channel._app.bot.sent_messages[0]["message_thread_id"] == 42


@pytest.mark.asyncio
async def test_send_reply_infers_topic_from_message_id_cache() -> None:
    config = TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], reply_to_message=True)
    channel = TelegramChannel(config, MessageBus())
    channel._app = _FakeApp(lambda: None)
    channel._message_threads[("123", 10)] = 42

    await channel.send(
        OutboundMessage(
            channel="telegram",
            chat_id="123",
            content="hello",
            metadata={"message_id": 10},
        )
    )

    assert channel._app.bot.sent_messages[0]["message_thread_id"] == 42
    assert channel._app.bot.sent_messages[0]["reply_parameters"].message_id == 10


@pytest.mark.asyncio
async def test_group_policy_mention_ignores_unmentioned_group_message() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="mention"),
        MessageBus(),
    )
    channel._app = _FakeApp(lambda: None)

    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    channel._handle_message = capture_handle
    channel._start_typing = lambda _chat_id: None

    await channel._on_message(_make_telegram_update(text="hello everyone"), None)

    assert handled == []
    assert channel._app.bot.get_me_calls == 1


@pytest.mark.asyncio
async def test_on_help_includes_restart_command() -> None:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=["*"], group_policy="open"),
        MessageBus(),
    )
    update = _make_telegram_update(text="/help", chat_type="private")
    update.message.reply_text = AsyncMock()

    await channel._on_help(update, None)

    update.message.reply_text.assert_awaited_once()
    help_text = update.message.reply_text.await_args.args[0]
    assert "/restart" in help_text
