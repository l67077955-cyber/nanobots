"""Mod contract — the unit of extension for nanobot orchestration.

A mod is a small, isolated behavioural add-on that subscribes to orchestration
events (see ``nanobot/groupchat/orchestra/events.py`` for the catalogue).
Mods never import engine internals or monkey-patch core code; they receive
capabilities through :class:`ModContext` and event payloads.

Authoring (also documented in ``docs/MOD_PLUGIN_GUIDE.md``)::

    from nanobot.mods.base import Mod

    class MyMod(Mod):
        name = "mymod"
        description = "what it does"

        async def on_user_message_delivered(self, *, message, delivered_to, **kw):
            ...

Event ``user:message_delivered`` binds to method ``on_user_message_delivered``
(``:`` and ``-`` become ``_``; every handler accepts ``**kw`` for forward
compatibility — new payload fields must never break a mod).

Tier rules (enforced by review, not machinery):
- Tier 1 (observe): read payloads, send displays via ``ctx.send``, write files.
- Tier 2 (filter): payloads may carry mutable containers (e.g. ``inject``)
  that the emitter applies afterwards — append to them, never replace.
- Tier 3 (tools): not yet available.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, ClassVar

from loguru import logger


class ModContext:
    """The capability handle a mod receives in :meth:`Mod.start`.

    Deliberately tiny: bus access for extra subscriptions, the mod's merged
    config, and a display send. No engine internals.
    """

    def __init__(
        self,
        bus: Any,
        config: dict[str, Any],
        send: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        self._send = send
        self.log = logger.bind(mod="")

    @property
    def bus(self) -> Any:
        return self._bus

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    async def send(self, text: str) -> None:
        """Show *text* to the user through the engine's display channel."""
        if self._send is not None:
            try:
                await self._send(text)
            except Exception as e:  # noqa: BLE001 — display must never kill a mod
                logger.error("mods: ctx.send failed: {}", e)


class Mod:
    """Base class every mod inherits from."""

    name: ClassVar[str] = "base"
    version: ClassVar[str] = "0.1"
    description: ClassVar[str] = ""

    def default_config(self) -> dict[str, Any]:
        """Config defaults; merged under ``~/.nanobot/mods.json`` overrides."""
        return {}

    async def start(self, ctx: ModContext) -> None:
        """Called once when the mod is enabled. Subscribe extra events here."""

    async def stop(self) -> None:
        """Called once on shutdown; release resources here."""

    # ── Handler discovery ──────────────────────────────────────────────────

    def handlers(self) -> dict[str, Callable[..., Awaitable[None]]]:
        """Map event names to bound async methods.

        Default discovery: ``user:message_delivered`` → ``on_user_message_delivered``
        (resolved against the event catalogue; ``on_*`` methods that match no
        catalogue event are private helpers and ignored).
        """
        result: dict[str, Callable[..., Awaitable[None]]] = {}
        for attr in dir(self):
            if not attr.startswith("on_"):
                continue
            fn = getattr(self, attr)
            if not callable(fn):
                continue
            event = _resolve_event_name(attr)
            if event:
                result[event] = fn
        return result


def _resolve_event_name(attr: str) -> str:
    """on_user_message_delivered → user:message_delivered (catalogue-driven)."""
    from nanobot.groupchat.orchestra.events import EVENTS
    stem = attr[3:]
    for event in EVENTS:
        if event.replace(":", "_").replace("-", "_") == stem:
            return event
    return ""
