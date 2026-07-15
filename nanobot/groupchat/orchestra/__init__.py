"""Deprecated parent package name: use nanobot.groupchat.runtime.

Module basenames (broadcast, engine, …) are unchanged.
"""
from nanobot.groupchat.runtime import (  # noqa: F401
    GroupChatEngine,
    broadcast_round,
    run_round,
    direct_chat,
    run_loop,
)
