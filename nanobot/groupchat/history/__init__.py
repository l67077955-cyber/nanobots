"""Groupchat history management package."""

import warnings

from nanobot.core.history import History
from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.persistence import GroupChatState

# Backward compatibility: HistoryContext is deprecated but still exported
# for existing tests. Use History from nanobot.core.history instead.
def __getattr__(name: str):
    if name == "HistoryContext":
        warnings.warn(
            "HistoryContext is deprecated. Use nanobot.core.history.History instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from nanobot.groupchat.history.context import HistoryContext as _HC
        return _HC
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["history_settings", "GroupChatState", "History"]
