"""Deprecated: use ``nanobot.gateway``.

This package re-exports gateway dispatch/inbox so old imports keep working.
Do not confuse with ``nanobot.groupchat.runtime`` (multi-agent logic layer).
"""
from nanobot.gateway import *  # noqa: F403
from nanobot.gateway import (  # noqa: F401
    SLASH_COMMANDS,
    InboundDispatcher,
    parse_slash_command,
    inbox_poller,
    start_inbox_poller,
)
