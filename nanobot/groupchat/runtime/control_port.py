"""Control surface for Telegram agent/group/prompt admin (not full engine).

Agent admin callbacks depend on this port. Live chat scheduling (mailbox,
broadcast tasks, busy/idle) stays internal to GroupChatEngine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nanobot.core.history import History
from nanobot.groupchat.context.settings_view import HistorySettingsView


@runtime_checkable
class GroupChatControlPort(HistorySettingsView, Protocol):
    """Settings + agent administration surface.

    Extends HistorySettingsView with registry mutations and prompt builder.
    Does **not** expose mailbox / runners / tool_loop.
    """

    registry: dict[str, dict[str, Any]]

    @property
    def leader(self) -> str | None: ...

    @property
    def provider(self) -> Any: ...

    @property
    def prompt_builder(self) -> Any: ...

    @property
    def workspace(self) -> Path: ...

    @property
    def config(self) -> Any: ...

    @property
    def request_log(self) -> list[dict[str, Any]]: ...

    def add_agent(self, name: str) -> str: ...

    def remove_agent(self, name: str) -> str: ...

    def delete_agent(self, name: str) -> Any: ...

    def set_leader(self, name: str | None) -> str: ...

    def load_group(self, name: str) -> str: ...

    def delete_group(self, name: str) -> str: ...

    def save_group(self, name: str) -> str: ...

    def save_active(self) -> None: ...

    def reorder_agents(self, names: list[str]) -> None: ...
