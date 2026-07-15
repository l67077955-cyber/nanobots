"""Deprecated parent package — use ``nanobot.groupchat.runtime``.

orchestra/ and runtime/ were overlapping names for the same execution layer.
All implementations live in runtime; this package only re-exports.
"""
from nanobot.groupchat.runtime import (  # noqa: F401
    GroupChatEngine,
    broadcast_round,
    run_round,
    direct_chat,
    run_loop,
)
