"""Groupchat runtime -- primary execution layer.

Prefer this package over legacy nanobot.groupchat.orchestra shims.

Exports: GroupChatEngine, run_round, broadcast_round, direct_chat, run_loop.
"""

from nanobot.groupchat.runtime.engine import GroupChatEngine
from nanobot.groupchat.runtime.round import run_round, broadcast_round
from nanobot.groupchat.runtime.direct import direct_chat
from nanobot.groupchat.runtime.loop import run_loop

__all__ = ["GroupChatEngine", "run_round", "broadcast_round", "direct_chat", "run_loop"]
