"""Command router for priority/exact/prefix slash commands (and CommandContext)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from nanobot.bus.events import OutboundMessage


if TYPE_CHECKING:
    # Avoid circular imports at runtime
    pass


@dataclass
class CommandContext:
    """Context passed to all command handlers."""
    msg: Any
    session: Any = None
    key: str = ""
    raw: str = ""
    loop: Any = None
    args: str = ""  # remainder after the command prefix


Handler = Callable[[CommandContext], Awaitable[OutboundMessage | None]]


class CommandRouter:
    """Three-tier command router.

    1. priority — checked first, exact match, bypass normal dispatch lock
       (used for /stop, /restart etc.)
    2. exact — exact command match
    3. prefix — longest prefix match
    """

    def __init__(self) -> None:
        self._priority: dict[str, Handler] = {}
        self._exact: dict[str, Handler] = {}
        self._prefix: list[tuple[str, Handler]] = []

    def priority(self, cmd: str, handler: Handler) -> None:
        self._priority[cmd] = handler

    def exact(self, cmd: str, handler: Handler) -> None:
        self._exact[cmd] = handler

    def prefix(self, prefix: str, handler: Handler) -> None:
        # Keep longer prefixes first for matching
        self._prefix.append((prefix, handler))
        self._prefix.sort(key=lambda p: len(p[0]), reverse=True)

    async def dispatch(self, ctx: CommandContext) -> OutboundMessage | None:
        """Dispatch a command context to the appropriate handler."""
        content = (ctx.msg.content or "").strip()
        raw = ctx.raw or content

        # 1. Priority exact
        if raw in self._priority:
            return await self._priority[raw](ctx)

        # 2. Exact
        if raw in self._exact:
            return await self._exact[raw](ctx)

        # 3. Prefix (longest first)
        for pref, handler in self._prefix:
            if raw.startswith(pref):
                # Provide .args for prefix handlers
                ctx.args = raw[len(pref):].strip()
                return await handler(ctx)

        return None

    def is_dispatchable_command(self, text: str) -> bool:
        """Quick check if a string looks like a registered command."""
        t = (text or "").strip()
        if t in self._priority or t in self._exact:
            return True
        return any(t.startswith(p) for p, _ in self._prefix)
