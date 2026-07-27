"""Tests for the ChatUI Protocol abstraction (P2.1, P2.5).

Verifies that groupchat's display layer can work with any ChatUI implementation,
not just Telegram. Uses a minimal in-memory ChatUI to prove cross-channel
capability.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.display.ui import ChatUI


class InMemoryChatUI:
    """Minimal ChatUI for testing — records all sends/edits in-memory.

    Simulates a channel that DOES support editing (like Telegram).
    """

    def __init__(self) -> None:
        self._messages: dict[int, str] = {}
        self._next_id = 0
        self.sends: list[str] = []
        self.edits: list[tuple[int, str]] = []

    @property
    def supports_edit(self) -> bool:
        return True

    async def send(self, text: str) -> int | None:
        self._next_id += 1
        msg_id = self._next_id
        self._messages[msg_id] = text
        self.sends.append(text)
        return msg_id

    async def edit(self, msg_id: int | None, text: str) -> None:
        if msg_id is None:
            return
        self._messages[msg_id] = text
        self.edits.append((msg_id, text))


class NoEditChatUI:
    """Minimal ChatUI for a channel WITHOUT edit support (e.g., email, CLI).

    edit() is a no-op; send() always returns None (no msg_id).
    """

    def __init__(self) -> None:
        self.sends: list[str] = []

    @property
    def supports_edit(self) -> bool:
        return False

    async def send(self, text: str) -> int | None:
        self.sends.append(text)
        return None

    async def edit(self, msg_id: int | None, text: str) -> None:
        # No-op for channels without edit support
        pass


def test_chatui_protocol_is_runtime_checkable():
    """ChatUI should be a runtime_checkable Protocol."""
    ui = InMemoryChatUI()
    assert isinstance(ui, ChatUI)


def test_inmemory_chatui_send_returns_id():
    """send() returns a unique message ID for later editing."""
    ui = InMemoryChatUI()

    id1 = asyncio.run(ui.send("hello"))
    id2 = asyncio.run(ui.send("world"))

    assert id1 == 1
    assert id2 == 2
    assert ui.sends == ["hello", "world"]


def test_inmemory_chatui_edit_updates_message():
    """edit() updates the stored message content."""
    ui = InMemoryChatUI()

    msg_id = asyncio.run(ui.send("original"))
    asyncio.run(ui.edit(msg_id, "edited"))

    assert ui._messages[msg_id] == "edited"
    assert ui.edits == [(msg_id, "edited")]


def test_noedit_chatui_degrades_gracefully():
    """A channel without edit support: send returns None, edit is no-op."""
    ui = NoEditChatUI()

    assert not ui.supports_edit

    msg_id = asyncio.run(ui.send("test"))
    assert msg_id is None  # no editable ID

    # edit() should not raise even with None msg_id
    asyncio.run(ui.edit(None, "ignored"))

    assert ui.sends == ["test"]


def test_engine_set_ui_wires_callbacks():
    """Engine.set_ui() should wire send/edit from a ChatUI adapter."""
    from nanobot.groupchat.orchestra.engine import GroupChatEngine
    from nanobot.groupchat.config import GroupChatConfig

    # Build a minimal engine (no I/O)
    config = GroupChatConfig()
    engine = GroupChatEngine.__new__(GroupChatEngine)
    # Manually init only the fields set_ui touches
    engine._send_fn = None
    engine._edit_fn = None
    engine._send_and_get_id_fn = None
    engine._ui = None

    ui = InMemoryChatUI()
    engine.set_ui(ui)

    assert engine._ui is ui
    assert engine._send_fn == ui.send  # send wired (bound method)
    assert engine.has_send_fn

    # Edit-capable UI → edit callbacks wired
    assert engine._edit_fn == ui.edit
    assert engine._send_and_get_id_fn == ui.send
    assert engine.has_edit_fn


def test_engine_set_ui_noedit_skips_edit_callbacks():
    """Engine.set_ui() with a no-edit UI should not wire edit callbacks."""
    from nanobot.groupchat.orchestra.engine import GroupChatEngine

    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine._send_fn = None
    engine._edit_fn = None
    engine._send_and_get_id_fn = None
    engine._ui = None

    ui = NoEditChatUI()
    engine.set_ui(ui)

    assert engine._send_fn == ui.send  # send always wired (bound method)
    assert engine.has_send_fn

    # No-edit UI → edit callbacks stay None
    assert engine._edit_fn is None
    assert engine._send_and_get_id_fn is None
    assert not engine.has_edit_fn
