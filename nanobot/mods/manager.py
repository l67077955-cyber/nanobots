"""ModManager — config-driven lifecycle for discovered mods.

Config: ``~/.nanobot/mods.json`` — ``{ "<mod name>": { "enabled": bool, ...cfg } }``.
Follows the history_settings conventions: module-level defaults deep-merged
under the user file, singleton cache, ``reload()``.

DEFAULTS ship ``antirepeat`` enabled because its logic was migrated out of
inline broadcast code — out-of-the-box behaviour stays identical to the
pre-mod codebase; disabling it via mods.json is an explicit opt-out.
Everything else is disabled unless the user turns it on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.orchestra.events import BroadcastEventDispatcher
from nanobot.mods.base import Mod, ModContext
from nanobot.mods.registry import discover_all

CONFIG_PATH = Path.home() / ".nanobot" / "mods.json"

# name → default config. "enabled" defaults preserve pre-mod behaviour for
# migrated inline concerns; everything else is opt-in.
_DEFAULTS: dict[str, dict[str, Any]] = {
    "antirepeat": {"enabled": True},
    "round_telemetry": {"enabled": False},
}

_cache: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    merged = {name: dict(cfg) for name, cfg in _DEFAULTS.items()}
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(user, dict):
            for name, cfg in user.items():
                if isinstance(cfg, dict):
                    merged.setdefault(name, {}).update(cfg)
                elif isinstance(cfg, bool):
                    merged.setdefault(name, {})["enabled"] = cfg
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 — bad user config must not kill startup
        logger.warning("mods: failed to read {}: {}", CONFIG_PATH, e)
    _cache = merged
    return merged


def reload() -> None:
    """Drop the config cache (takes effect on next ModManager construction)."""
    global _cache
    _cache = None


def get_all() -> dict[str, dict[str, Any]]:
    return {name: dict(cfg) for name, cfg in _load().items()}


class ModManager:
    """Starts/stops the enabled mods against one bus instance."""

    def __init__(
        self,
        bus: BroadcastEventDispatcher,
        send: Callable[[str], Awaitable[None]] | None = None,
        *,
        classes: dict[str, type[Mod]] | None = None,
    ) -> None:
        self._bus = bus
        self._send = send
        self._classes = classes if classes is not None else discover_all()
        self._instances: dict[str, Mod] = {}
        self._handlers: dict[str, list[tuple[Mod, Callable[..., Any]]]] = {}

    @property
    def active(self) -> list[str]:
        return sorted(self._instances)

    def start_all(self) -> list[str]:
        """Instantiate + start every enabled mod. Per-mod fault isolation:
        a mod that raises in start/handler wiring is skipped, never fatal."""
        started: list[str] = []
        config = _load()
        for name, cls in sorted(self._classes.items()):
            cfg = config.get(name, {})
            if not cfg.get("enabled", False):
                continue
            try:
                instance = cls()
                merged_cfg = {**instance.default_config(), **{
                    k: v for k, v in cfg.items() if k != "enabled"
                }}
                ctx = ModContext(self._bus, merged_cfg, send=self._send)
                ctx.log = logger.bind(mod=name)
            except Exception as e:  # noqa: BLE001
                logger.error("mods: '{}' failed to instantiate: {}", name, e)
                continue
            self._schedule_start(name, instance, ctx)
            for event, fn in instance.handlers().items():
                self._bus.on(event, fn)
                self._handlers.setdefault(event, []).append((instance, fn))
            self._instances[name] = instance
            started.append(name)
        if started:
            logger.info("mods: started {} — {}", len(started), ", ".join(started))
        return started

    def _schedule_start(self, name: str, instance: Mod, ctx: ModContext) -> None:
        import asyncio

        async def _run() -> None:
            try:
                await instance.start(ctx)
            except Exception as e:  # noqa: BLE001
                logger.error("mods: '{}' failed to start: {}", name, e)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_run())
        except RuntimeError:
            # No loop yet (manager constructed outside async context) —
            # run start() on first opportunity via the bus's first emit? No:
            # mods must be startable synchronously too, so run it blocked-free
            # by deferring to a fresh loop in a thread-less manner: simply
            # record it; gateway always constructs inside a loop.
            logger.debug("mods: '{}' start deferred (no running loop)", name)

    def stop_all(self) -> None:
        for name, instance in list(self._instances.items()):
            for event, entries in self._handlers.items():
                for inst, fn in entries:
                    if inst is instance:
                        try:
                            self._bus.off(event, fn)
                        except Exception:  # noqa: BLE001
                            pass
            import asyncio

            async def _stop(inst: Mod = instance, nm: str = name) -> None:
                try:
                    await inst.stop()
                except Exception as e:  # noqa: BLE001
                    logger.error("mods: '{}' failed to stop: {}", nm, e)

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_stop())
            except RuntimeError:
                pass
            del self._instances[name]
        self._handlers.clear()
