"""Group chat display layer — channel-agnostic rendering helpers.

- ``display``: pure text rendering (status panels, tool lines, broadcast msgs)
- ``broadcast_view``: renders broadcast events to the channel (pure, no control flow)
- ``streaming``: throttled streaming-message editing
- ``status_panel``: ``StatusPanel`` protocol + channel implementations
"""

from nanobot.groupchat.display.status_panel import (
    DiscordStatusPanel,
    MatrixStatusPanel,
    NullStatusPanel,
    StatusPanel,
    make_status_panel,
)

__all__ = [
    "StatusPanel",
    "NullStatusPanel",
    "DiscordStatusPanel",
    "MatrixStatusPanel",
    "make_status_panel",
]
