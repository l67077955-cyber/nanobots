"""Gateway services — inbound dispatch and inbox poller.

Prefer: ``from nanobot.gateway.dispatch import …``

Renamed from ``nanobot.runtime`` (removed) to avoid clashing with
``nanobot.groupchat.runtime``.
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
