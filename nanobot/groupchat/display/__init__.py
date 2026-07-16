"""View layer — message formatting, streaming UI, broadcast panels.

**Does:** turn events into user-visible text (Telegram/etc.).
**Does not:** import ``groupchat.runtime``; mutate History / busy.

Public modules
--------------
- ``display`` — tool/message formatters, icons, start banners
  (import as ``from nanobot.groupchat.display import display as _d``)
- ``streaming`` — StreamingDisplay
- ``broadcast_view`` — BroadcastView (callbacks injected by runtime)
- ``visibility`` — UI labels; rank *policy* lives in ``context.ranks``
  (re-exported from visibility for older call sites only)

Side effects (send, interrupt) must be injected as callbacks by
the runtime layer — never imported from runtime into this package.

Must not import groupchat.runtime (scheduling). Runtime injects
callbacks (e.g. BroadcastView.on_chatroom_send_ok) when side-effects
beyond rendering are required.
"""

from nanobot.groupchat.display.display import agent_badge, format_token_stats

__all__ = ["agent_badge", "format_token_stats"]
