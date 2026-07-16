"""Public re-export of groupchat config schema.

Implementation: ``groupchat.context.gc_config``. Prefer importing from
``nanobot.groupchat.context.gc_config`` in new groupchat-internal code.
"""
from nanobot.groupchat.context.gc_config import (  # noqa: F401
    GroupChatAgentConfig,
    GroupChatConfig,
)

__all__ = ["GroupChatAgentConfig", "GroupChatConfig"]
