"""Anti-repeat reminder mod.

Migrated from inline broadcast code (the ``[提醒] 你已经发表过上述观点``
injection before re-entering tool_loop after a wake-up). Listens to
``agent:reactivated`` and appends a reminder into the emitter-provided
``inject`` container when the tag is absent from the agent's recent
messages — the emitter turns each injected string into a system message.

Tier 2 (filter): mutates only the ``inject`` list; touches nothing else.
Enabled by default in manager DEFAULTS to preserve pre-mod behaviour.
"""

from __future__ import annotations

from typing import Any

from nanobot.mods.base import Mod

REMINDER_TAG = "[提醒]"


class AntiRepeatMod(Mod):
    name = "antirepeat"
    version = "1.0"
    description = "唤醒队友消息时注入防重复提醒（从内嵌代码迁移）"

    _window: int = 6  # overwritten from config in start(); class default so
    #                 # handlers are safe even if an event fires first

    def default_config(self) -> dict[str, Any]:
        return {"window": 6}

    async def on_agent_reactivated(
        self, *, agent: str, recent_texts: list[str] | None = None,
        inject: list[str] | None = None, **kw: Any,
    ) -> None:
        if inject is None:
            return
        recent = (recent_texts or [])[-self._window:]
        if any(REMINDER_TAG in t for t in recent):
            return
        inject.append(
            f"{REMINDER_TAG} 你（{agent}）已经发表过上述观点。"
            f"针对队友的新消息做出回应或补充新观点，不要重复已说的内容。"
        )

    async def start(self, ctx: Any) -> None:
        self._window = int(ctx.config.get("window", 6))
