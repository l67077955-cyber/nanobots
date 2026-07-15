"""Group chat package.

Preferred parents: runtime/ (execution), context/ (prompt+History plumbing),
display/ (surface). Legacy: orchestra->runtime, history->context shims.
"""

from nanobot.groupchat.runtime.engine import GroupChatEngine

__all__ = ["GroupChatEngine"]
