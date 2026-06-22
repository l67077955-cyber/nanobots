"""Tests for gateway-integrated dashboard and chat hub."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from nanobot.channels.web import WebChannel, WebConfig
from nanobot.runtime.chat_hub import ChatHub
from nanobot.runtime.dashboard import DashboardHandler, resolve_repo


def test_resolve_repo_finds_nanobot_src() -> None:
    root = resolve_repo(None)
    assert (root / "nanobot").is_dir()


def test_chat_hub_push_and_poll() -> None:
    hub = ChatHub(chat_id="dashboard")
    hub.push({"type": "message", "role": "user", "content": "hi"})
    events = hub.events_after(0)
    assert len(events) == 1
    assert events[0]["content"] == "hi"
    assert events[0]["id"] == 1


def test_chat_hub_send_calls_dispatch() -> None:
    hub = ChatHub()
    loop = MagicMock()
    done_callbacks = []

    async def dispatch(chat_id: str, sender: str, content: str) -> None:
        return None

    future = MagicMock()
    future.add_done_callback.side_effect = lambda cb: done_callbacks.append(cb)

    with patch("asyncio.run_coroutine_threadsafe", return_value=future) as scheduled:
        hub.attach(loop, dispatch)
        assert hub.send("/help") is True
        scheduled.assert_called_once()
        scheduled.call_args.args[0].close()
        assert done_callbacks == [hub._record_dispatch_error]


def test_web_channel_pushes_hub_events() -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    hub = ChatHub()
    ch = WebChannel(WebConfig(), bus)
    ch.set_chat_hub(hub)

    async def run() -> None:
        from nanobot.bus.events import OutboundMessage

        await ch._emit_chat("dashboard", {"type": "message", "role": "user", "content": "ping"})
        await ch.send(OutboundMessage(channel="web", chat_id="dashboard", content="pong"))

    asyncio.run(run())
    assert len(hub.events_after(0)) == 2


def test_dashboard_handler_wiring() -> None:
    handler = DashboardHandler
    handler.repo = resolve_repo(None)
    handler.refresh_hint_s = 5
    handler.auth_password = None
    handler.auth_token = None
    handler.sessions = {}
    handler.sessions_lock = threading.Lock()
    handler.chat_hub = ChatHub()
    handler.gateway_port = 18790
    assert handler.chat_hub is not None
