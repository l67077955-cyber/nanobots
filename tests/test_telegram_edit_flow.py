"""Regression tests for the interactive edit-flow lifecycle.

Covers three fixes:
- ``_begin_edit`` stamps ``_edit_state_since`` so a wizard's first typed
  input is staged (previously it fell through to the group chat), and a
  stale stamp left by a PREVIOUS session no longer kills a newly created
  one.
- ``emc_mi:`` create-model buttons parse their 4-segment payload.
- ``_on_callback`` / ``_on_settings`` enforce the ``allow_from`` allowlist.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import TelegramChannel, TelegramConfig
from tests.test_telegram_channel import _make_telegram_update


def _channel(allow_from=("*",)) -> TelegramChannel:
    channel = TelegramChannel(
        TelegramConfig(enabled=True, token="123:abc", allow_from=list(allow_from), group_policy="open"),
        MessageBus(),
    )
    channel._app = None
    return channel


def _engine_mock():
    return SimpleNamespace(
        active_agents=["kirk"],
        inject=AsyncMock(),
        has_send_fn=True,
        has_edit_fn=True,
        has_on_round_done=True,
        # Re-binding callbacks when the requesting chat differs calls these
        # setters on the real engine; the mock must accept them too.
        set_send_fn=AsyncMock(),
        set_edit_fn=AsyncMock(),
        set_tool_context=AsyncMock(),
    )


def _callback_update(data: str, user_id: int = 7, username: str = "alice"):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, username=username),
        message=SimpleNamespace(chat_id=-100123),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query)


# ── _begin_edit lifecycle ──────────────────────────────────────────────────

def test_begin_edit_stamps_session_time() -> None:
    channel = _channel()
    before = time.time()
    channel._begin_edit("-100123", {"field": "create_name", "mode": "create"})
    assert channel._edit_state["-100123"]["field"] == "create_name"
    assert channel._edit_state_since["-100123"] >= before


@pytest.mark.asyncio
async def test_wizard_first_input_is_staged_not_injected() -> None:
    """First typed input after the wizard panel must be staged for confirm,
    never injected into the group chat as a normal message."""
    channel = _channel()
    channel._groupchat_engine = _engine_mock()
    channel._stage_confirm = AsyncMock()
    channel._begin_edit("-100123", {"field": "create_name", "mode": "create"})

    await channel._on_message(_make_telegram_update(text="MyNewAgent"), None)

    assert channel._pending_input["-100123"]["content"] == "MyNewAgent"
    channel._groupchat_engine.inject.assert_not_called()
    channel._stage_confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_stamp_from_previous_session_does_not_kill_new_one() -> None:
    """A leftover ``_edit_state_since`` from an old ended session must not
    false-timeout a freshly created edit session's first input."""
    channel = _channel()
    channel._groupchat_engine = _engine_mock()
    channel._stage_confirm = AsyncMock()
    # Old session came and went without clearing its stamp.
    channel._edit_state_since["-100123"] = time.time() - 60 * 25
    channel._begin_edit("-100123", {"field": "create_name", "mode": "create"})

    await channel._on_message(_make_telegram_update(text="SecondAttempt"), None)

    assert channel._pending_input["-100123"]["content"] == "SecondAttempt"
    assert "-100123" in channel._edit_state
    channel._groupchat_engine.inject.assert_not_called()


@pytest.mark.asyncio
async def test_genuinely_stale_session_falls_through_to_normal_routing() -> None:
    """An edit session untouched for >20 min is dropped and the message
    routes to the group chat as normal, with a notice."""
    channel = _channel()
    channel._groupchat_engine = _engine_mock()
    channel._begin_edit("-100123", {"field": "prompt_edit"})
    channel._edit_state_since["-100123"] = time.time() - 60 * 25

    update = _make_telegram_update(text="just chatting")
    update.message.reply_text = AsyncMock()
    await channel._on_message(update, None)

    assert "-100123" not in channel._edit_state
    assert "-100123" not in channel._pending_input
    channel._groupchat_engine.inject.assert_called_once_with("just chatting")
    update.message.reply_text.assert_awaited_once()


# ── emc_mi payload contract ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emc_mi_model_button_selects_model_and_advances() -> None:
    """emc_mi:<agent>:<prov>:<idx> (4 segments) must select the cached model
    and advance the wizard to the persona step."""
    channel = _channel()
    channel._emc_model_cache = {"Kirk:openrouter": ["prov/big-model", "prov/other"]}
    channel._begin_edit("-100123", {"agent": "Kirk", "field": "create_model"})

    update = _callback_update("emc_mi:Kirk:openrouter:0")
    await channel._on_callback(update, None)

    state = channel._edit_state["-100123"]
    assert state["model"] == "prov/big-model"
    assert state["field"] == "create_persona"
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "prov/big-model" in text


# ── callback / settings ACL ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_denied_for_user_outside_allowlist() -> None:
    channel = _channel(allow_from=["42|bob"])
    update = _callback_update("dac:Kirk", user_id=7, username="alice")
    await channel._on_callback(update, None)

    update.callback_query.edit_message_text.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()
    assert "无权限" in update.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_callback_allowed_for_allowlisted_user() -> None:
    channel = _channel(allow_from=["7|alice"])
    channel._emc_model_cache = {"Kirk:openrouter": ["prov/big-model"]}
    channel._begin_edit("-100123", {"agent": "Kirk", "field": "create_model"})

    update = _callback_update("emc_mi:Kirk:openrouter:0", user_id=7, username="alice")
    await channel._on_callback(update, None)

    update.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_settings_denied_for_user_outside_allowlist() -> None:
    channel = _channel(allow_from=["42|bob"])
    update = _make_telegram_update(text="/settings")
    update.message.reply_text = AsyncMock()
    await channel._on_settings(update, None)
    update.message.reply_text.assert_not_awaited()
