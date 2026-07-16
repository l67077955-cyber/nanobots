"""Prompt / policy helpers that *project* History — not a second store.

**Context ownership:** ``nanobot.core.history.History`` only.

This package provides:
- PromptBuilder — History → LLM message list (read projection)
- ranks / history_settings / pruning — policy knobs for that projection
- persistence — disk snapshots of History (I/O)
- conversation — thin port onto History for collaboration code

Scheduling, busy/idle, mailbox, cycle loop → ``groupchat.runtime``.
Rendering → ``groupchat.display``.
"""

from nanobot.core.history import History
from nanobot.groupchat.context import history_settings
from nanobot.groupchat.context.persistence import GroupChatState

__all__ = ["history_settings", "GroupChatState", "History"]
