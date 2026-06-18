"""Agent core — multi-agent collaboration via GroupChatEngine.

The original single-agent ``agent/loop.py`` was superseded by
``nanobot.groupchat.orchestra.engine.GroupChatEngine``, which is the
runtime core for both 1-on-1 and group chat modes.

Import from here for a stable entry point:

    from nanobot.agent import GroupChatEngine
"""

from nanobot.groupchat.orchestra.engine import GroupChatEngine

__all__ = ["GroupChatEngine"]