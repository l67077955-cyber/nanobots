"""Tests for gateway-integrated dashboard and chat hub."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from nanobot.channels.web import WebChannel, WebConfig
from nanobot.runtime.chat_controls import (
    apply_control_action,
    command_catalog,
    providers_panel,
    runtime_control_commands,
)
from nanobot.runtime.chat_events import OutboundMirrorSink
from nanobot.runtime.chat_hub import ChatHub
from nanobot.runtime.dashboard import DashboardHandler, resolve_repo
from nanobot.runtime.dispatch import SLASH_COMMANDS


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

    async def dispatch(chat_id: str, sender: str, content: str, **kwargs) -> None:
        return None

    future = MagicMock()
    future.add_done_callback.side_effect = lambda cb: done_callbacks.append(cb)

    with patch("asyncio.run_coroutine_threadsafe", return_value=future) as scheduled:
        hub.attach(loop, dispatch)
        assert hub.send("/help") is True
        scheduled.assert_called_once()
        scheduled.call_args.args[0].close()
        assert done_callbacks == [hub._record_dispatch_error]


def test_chat_hub_can_send_without_user_echo() -> None:
    hub = ChatHub()
    loop = MagicMock()

    async def dispatch(chat_id: str, sender: str, content: str, **kwargs) -> None:
        return None

    future = MagicMock()
    future.add_done_callback = MagicMock()
    with patch("asyncio.run_coroutine_threadsafe", return_value=future) as scheduled:
        hub.attach(loop, dispatch)
        assert hub.send("/agents", echo=False) is True
        coro = scheduled.call_args.args[0]
        assert coro.cr_frame.f_locals["kwargs"]["emit_user"] is False
        coro.close()


def test_dashboard_signed_session_survives_memory_reset() -> None:
    handler = DashboardHandler
    handler.auth_password = "pw"
    handler.sessions = {}
    handler.sessions_lock = threading.Lock()

    sid = handler._issue_session(handler)
    handler.sessions = {}

    assert handler._signed_session_valid(handler, sid) is True


def test_chat_command_catalog_covers_dispatcher_commands() -> None:
    names = {item["name"] for item in command_catalog()["commands"]}
    assert set(SLASH_COMMANDS).issubset(names)


def test_agent_active_control_translates_to_runtime_commands(tmp_path) -> None:
    root = tmp_path / ".nanobot"
    root.mkdir()
    (root / "active_agents.json").write_text('["Harper", "Kirk"]', encoding="utf-8")

    commands = runtime_control_commands(
        {"action": "agent_active", "value": ["Kirk", "Ada"]},
        home=root,
    )

    assert commands == ["/addagent Ada", "/removeagent Harper"]


def test_leader_control_translates_to_runtime_command(tmp_path) -> None:
    commands = runtime_control_commands(
        {"action": "leader", "value": "Kirk"},
        home=tmp_path,
    )

    assert commands == ["/setleader Kirk"]


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


def test_web_channel_mirror_sink_publishes_outbound() -> None:
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    bus.publish_inbound = AsyncMock()

    ch = WebChannel(WebConfig(), bus)
    ch.add_chat_sink(OutboundMirrorSink(bus, channel="telegram", chat_id="8008274300"))

    async def run() -> None:
        await ch._emit_chat("dashboard", {"type": "message", "role": "user", "content": "/agents"})

    asyncio.run(run())
    msg = bus.publish_outbound.await_args.args[0]
    assert msg.channel == "telegram"
    assert msg.chat_id == "8008274300"
    assert msg.content == "网页端: /agents"
    assert msg.metadata["_mirror_from"] == "web"


def test_providers_panel_and_save(tmp_path, monkeypatch) -> None:
    root = tmp_path / ".nanobot"
    root.mkdir()
    pm_path = root / "providers_models.json"
    pm_path.write_text(
        '{"providers": {"demo": {"url": "https://old.example/v1", "apiKey": "sk-old"}}, "models": {"demo": []}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot.runtime.chat_controls.Path.home", lambda: tmp_path)

    panel = providers_panel()
    assert panel["providers"][0]["name"] == "demo"
    assert panel["providers"][0]["url"] == "https://old.example/v1"

    result = apply_control_action({
        "action": "provider_save",
        "provider": "demo",
        "url": "https://new.example/v1",
        "api_key": "sk-new",
    })
    assert result["ok"] is True
    import json

    saved = json.loads(pm_path.read_text(encoding="utf-8"))
    assert saved["providers"]["demo"]["url"] == "https://new.example/v1"
    assert saved["providers"]["demo"]["apiKey"] == "sk-new"


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
