"""Groupchat **context** layer — shared transcript plumbing & prompt views.

Owns: prompt assembly, persistence snapshots, compression settings, pruning
helpers, agent loading. Transcript truth object is ``nanobot.core.history.History``;
scheduling/busy/mailbox live in ``groupchat.runtime``.

Formerly named ``groupchat.history`` (kept as a re-export shim).
"""

from nanobot.core.history import History
from nanobot.groupchat.context import history_settings
from nanobot.groupchat.context.persistence import GroupChatState

__all__ = ["history_settings", "GroupChatState", "History"]
