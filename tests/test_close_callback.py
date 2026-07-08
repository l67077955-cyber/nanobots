"""Tests for the unified `close` inline-keyboard callback.

`close` must delete the panel message and clear any pending input state for
the chat, falling back to a collapsed "已关闭" text when deletion is not
possible. See `callbacks/core.py`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.channels.telegram.callbacks.core import CallbackCoreMixin


class _FakeMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class _FakeQuery:
    def __init__(self, data: str, *, chat_id: int = 1, delete_fails: bool = False) -> None:
        self.data = data
        self.message = _FakeMessage(chat_id)
        self.from_user = None  # _on_callback logs query.from_user.id
        self._delete_fails = delete_fails
        self.deleted = False
        self.edited = None
        self.answered = False

    async def answer(self, *args, **kwargs) -> None:
        self.answered = True

    async def delete_message(self) -> None:
        if self._delete_fails:
            raise RuntimeError("cannot delete")
        self.deleted = True

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edited = text


class _Channel(CallbackCoreMixin):
    """Minimal host exposing only what `_on_callback` touches for `close`."""

    def __init__(self) -> None:
        self._edit_state: dict = {}


async def _fire(data: str, **kw) -> tuple[_FakeQuery, _Channel]:
    query = _FakeQuery(data, **kw)
    chan = _Channel()
    chan._edit_state["1"] = {"field": "pending"}  # simulate an input state
    await chan._on_callback(SimpleNamespace(callback_query=query), context=None)
    return query, chan


@pytest.mark.asyncio
async def test_close_deletes_message_and_clears_edit_state() -> None:
    query, chan = await _fire("close")
    assert query.answered is True
    assert query.deleted is True
    assert query.edited is None
    assert chan._edit_state == {}


@pytest.mark.asyncio
async def test_close_falls_back_to_text_when_delete_fails() -> None:
    query, chan = await _fire("close", delete_fails=True)
    assert query.deleted is False
    assert query.edited == "✅ 已关闭"
    assert chan._edit_state == {}


@pytest.mark.asyncio
async def test_noop_does_not_delete_or_clear_state() -> None:
    query, chan = await _fire("noop")
    assert query.deleted is False
    assert query.edited is None
    assert chan._edit_state == {"1": {"field": "pending"}}
