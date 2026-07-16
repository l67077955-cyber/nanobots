"""Group chat package.

Preferred parents: runtime/ (execution), context/ (prompt+History plumbing),
display/ (surface).
"""

from nanobot.groupchat.runtime.engine import GroupChatEngine

__all__ = ["GroupChatEngine"]
