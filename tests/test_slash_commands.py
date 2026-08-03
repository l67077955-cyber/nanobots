"""Web channel uses the same command handlers as Telegram."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nanobot.channels.web_shim import WebChannel
from nanobot.channels.web_shim import WebContext, WebReplyMessage, WebUpdate, WebUser
from nanobot.runtime.dispatch import SLASH_COMMANDS


def test_runtime_command_map_has_core_commands() -> None:
    for cmd in ("new", "clear", "stop", "agents", "addagent", "help", "log", "groupchat"):
        assert cmd in SLASH_COMMANDS


def test_web_forward_new_clears_history() -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    ch = WebChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    engine = MagicMock()
    engine._running = False
    engine.clear_history = MagicMock()
    ch.set_groupchat_engine(engine)

    update = WebUpdate(
        message=WebReplyMessage("dashboard", "/new", ch),
        effective_user=WebUser(id="web-user"),
    )
    asyncio.run(ch._forward_command(update, WebContext()))

    engine.clear_history.assert_called_once()
    bus.publish_outbound.assert_called()