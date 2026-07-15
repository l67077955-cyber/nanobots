"""Deprecated package name: use nanobot.groupchat.runtime.

Kept as re-exports so existing imports keep working.
"""
from nanobot.groupchat.runtime import (  # noqa: F401
    GroupChatEngine,
    run_round,
    broadcast_round,
    direct_chat,
    run_loop,
)
