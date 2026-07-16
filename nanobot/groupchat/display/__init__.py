"""Display / surface layer — formatting and streaming only.

Must not import groupchat.runtime (scheduling). Runtime injects
callbacks (e.g. BroadcastView.on_chatroom_send_ok) when side-effects
beyond rendering are required.
"""

from nanobot.groupchat.display.display import agent_badge, format_token_stats

__all__ = ["agent_badge", "format_token_stats"]
