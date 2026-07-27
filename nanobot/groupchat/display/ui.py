"""Chat UI abstraction layer for groupchat.

Defines the Protocol interface that any channel (Telegram, Feishu, CLI, etc.)
must implement to plug into groupchat's streaming display and status tracking.

Decouples groupchat core from Telegram-specific msg_id and callback patterns.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChatUI(Protocol):
    """Protocol for chat UI operations required by groupchat.

    Implementations provide send/edit capabilities with graceful degradation
    for channels that don't support editing (e.g., email, CLI).
    """

    @property
    def supports_edit(self) -> bool:
        """Whether this UI supports editing previously sent messages.

        If False, edit() should no-op or send a new message instead.
        StreamingDisplay and AgentStatusTracker use this to degrade gracefully.
        """
        ...

    async def send(self, text: str) -> int | None:
        """Send a text message and return its ID (or None if unavailable).

        The returned ID can be passed to edit() for in-place updates.
        Channels without message IDs should return None and ignore edit() calls.
        """
        ...

    async def edit(self, msg_id: int | None, text: str) -> None:
        """Edit a previously sent message by ID.

        If msg_id is None or editing is unsupported, this should no-op.
        Implementations should handle rate limiting (429) gracefully.
        """
        ...
