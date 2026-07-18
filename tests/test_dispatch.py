"""Tests for unified inbound dispatcher."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nanobot.channels.commands_core import CoreCommandsMixin
from nanobot.gateway.dispatch import SLASH_COMMANDS, InboundDispatcher, parse_slash_command


class _FakeWebHost(CoreCommandsMixin):
    """Minimal CommandHost stand-in for the removed WebChannel.

    Satisfies the dispatch.CommandHost protocol and reuses the real
    CoreCommandsMixin so /new /clear /stop behavior stays under test.
    """

    name = "web"

    def __init__(self, bus) -> None:
        self.bus = bus
        self._edit_state: dict[str, dict] = {}
        self._groupchat_engine = None

    # CommandHost protocol -------------------------------------------------
    def is_allowed(self, sender_id: str) -> bool:
        return True

    def _ensure_gc_send(self, chat_id: str) -> None:
        pass

    async def _handle_edit_input(self, chat_id: str, content: str) -> None:
        pass

    # Channel API ----------------------------------------------------------
    def set_groupchat_engine(self, engine) -> None:
        self._groupchat_engine = engine


def test_slash_command_map_has_core_commands() -> None:
    for cmd in ("new", "clear", "stop", "agents", "help", "groupchat"):
        assert cmd in SLASH_COMMANDS


def test_runtime_command_map_has_extended_commands() -> None:
    for cmd in ("new", "clear", "stop", "agents", "addagent", "help", "log", "groupchat"):
        assert cmd in SLASH_COMMANDS


def test_parse_slash_command_strips_bot_suffix() -> None:
    assert parse_slash_command("/new@MyBot args") == ("new", ["args"])


def test_dispatcher_new_clears_history() -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    ch = _FakeWebHost(bus)
    engine = MagicMock()
    engine._running = False
    engine.clear_history = MagicMock()
    ch.set_groupchat_engine(engine)

    dispatcher = InboundDispatcher()
    handled = asyncio.run(
        dispatcher.handle(ch, "dashboard", "web-user", "/new", bus=bus)
    )
    assert handled is True
    engine.clear_history.assert_called_once()
    bus.publish_outbound.assert_called()


def test_dispatcher_plain_text_goes_to_bus() -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    ch = _FakeWebHost(bus)
    ch.set_groupchat_engine(MagicMock())

    dispatcher = InboundDispatcher()
    handled = asyncio.run(
        dispatcher.handle(ch, "dashboard", "web-user", "hello", bus=bus)
    )
    assert handled is True
    bus.publish_inbound.assert_called_once()
    msg = bus.publish_inbound.call_args[0][0]
    assert msg.content == "hello"
    assert msg.channel == "web"


def test_web_and_telegram_share_command_table() -> None:
    assert SLASH_COMMANDS["new"] == "_forward_command"
    assert SLASH_COMMANDS["agents"] == "_on_agents"


def test_dispatcher_passes_args_to_telegram_context() -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    host = MagicMock()
    host.name = "telegram"
    host._edit_state = {}
    host._groupchat_engine = MagicMock()
    host.is_allowed = MagicMock(return_value=True)
    host._ensure_gc_send = MagicMock()

    captured: dict[str, list[str]] = {}

    async def _on_addagent(update, context):
        captured["args"] = list(context.args or [])

    host._on_addagent = _on_addagent

    update = MagicMock()
    context = MagicMock()
    context.args = None

    dispatcher = InboundDispatcher()
    handled = asyncio.run(
        dispatcher.handle(
            host,
            "123",
            "user-1",
            "/addagent alice",
            bus=bus,
            tg_update=update,
            tg_context=context,
        )
    )

    assert handled is True
    assert captured["args"] == ["alice"]
    bus.publish_inbound.assert_not_called()


def test_dispatcher_unknown_command_does_not_hit_bus() -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    ch = _FakeWebHost(bus)
    ch.set_groupchat_engine(MagicMock())

    dispatcher = InboundDispatcher()
    handled = asyncio.run(
        dispatcher.handle(ch, "dashboard", "web-user", "/notacommand", bus=bus)
    )

    assert handled is True
    bus.publish_inbound.assert_not_called()
    bus.publish_outbound.assert_called_once()
    assert "未知命令" in bus.publish_outbound.call_args[0][0].content
