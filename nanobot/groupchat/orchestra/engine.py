"""Legacy shim — implementation lives in nanobot.groupchat.runtime.

This module re-exports for backward compatibility. New code should import
from nanobot.groupchat.runtime (e.g. runtime.round, runtime.engine).
"""
from nanobot.groupchat.runtime.engine import *  # noqa: F403
from nanobot.groupchat.runtime.engine import GroupChatEngine
