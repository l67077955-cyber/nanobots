"""Gateway services — inbound dispatch and inbox poller.

Formerly ``nanobot.runtime`` (renamed to avoid clashing with
``nanobot.groupchat.runtime``, the multi-agent logic layer).

Prefer: ``from nanobot.gateway.dispatch import …``
Legacy ``nanobot.runtime`` remains a re-export shim.
"""

from nanobot.gateway.dispatch import (  # noqa: F401
    SLASH_COMMANDS,
    InboundDispatcher,
    parse_slash_command,
)
from nanobot.gateway.inbox import inbox_poller, start_inbox_poller  # noqa: F401

__all__ = [
    "SLASH_COMMANDS",
    "InboundDispatcher",
    "parse_slash_command",
    "inbox_poller",
    "start_inbox_poller",
]
