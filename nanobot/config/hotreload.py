"""Hot-reload support for runtime configuration.

The gateway loads ``config.json`` once at startup and builds the provider /
agent from it.  Without a restart, later edits (e.g. rotating an API key)
never take effect — this module closes that gap with an *on-demand* strategy:
before every LLM call, :class:`ConfigReloader` stats the watched files and,
only when an mtime changed, re-reads and re-applys the config in place.

Watched sources:
- ``~/.nanobot/config.json``          → provider credentials + agents.defaults
- ``~/.nanobot/agents/*/config.json`` → groupchat agent registry (models etc.)

Failure policy: a broken edit (invalid JSON / validation error) keeps the
previously running config and only logs a warning — the service never crashes
because of a bad config save.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pydantic
from loguru import logger

from nanobot.config.loader import get_config_path
from nanobot.config.schema import Config


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class ConfigReloader:
    """On-demand config reloader driven by file mtimes.

    ``maybe_reload()`` is intentionally cheap (a few ``stat`` calls) so it can
    run before every LLM request.  ``on_reload`` is invoked as
    ``on_reload(new_config, agents_changed: bool)`` after a successful reload.
    """

    def __init__(
        self,
        config: Config,
        config_path: Path | None = None,
        on_reload: Callable[[Config, bool], None] | None = None,
        agents_dir: Path | None = None,
    ):
        self._config = config
        self._path = Path(config_path) if config_path else get_config_path()
        self._on_reload = on_reload
        self._agents_dir = agents_dir
        self._config_mtime = _mtime(self._path)
        self._agents_mtime = self._scan_agents_mtime()

    @property
    def config(self) -> Config:
        return self._config

    def _scan_agents_mtime(self) -> float | None:
        """Latest mtime across per-agent config.json files (groupchat agents)."""
        if not self._agents_dir or not self._agents_dir.is_dir():
            return None
        latest: float | None = None
        try:
            for f in self._agents_dir.glob("*/config.json"):
                m = _mtime(f)
                if m is not None and (latest is None or m > latest):
                    latest = m
        except OSError:
            pass
        return latest

    def _diff_sections(self, old: Config, new: Config) -> list[str]:
        """Names of top-level config sections that changed (for logging)."""
        try:
            old_dump = old.model_dump(mode="json")
            new_dump = new.model_dump(mode="json")
        except Exception:
            return []
        return sorted(k for k in new_dump if old_dump.get(k) != new_dump.get(k))

    def maybe_reload(self) -> bool:
        """Reload config if watched files changed. Returns True if applied."""
        config_mtime = _mtime(self._path)
        agents_mtime = self._scan_agents_mtime()
        config_changed = config_mtime != self._config_mtime
        agents_changed = agents_mtime != self._agents_mtime

        # Advance markers regardless of outcome: a broken file is retried only
        # when it is saved again (mtime changes), not on every LLM call.
        self._config_mtime = config_mtime
        self._agents_mtime = agents_mtime

        if not (config_changed or agents_changed):
            return False

        new_config = self._config
        if config_changed:
            # Strict parse: loader.load_config() silently falls back to
            # defaults on error, which would be dangerous here.
            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                new_config = Config.model_validate(data)
            except (OSError, json.JSONDecodeError, pydantic.ValidationError) as e:
                logger.warning("Hot-reload: invalid config {}, keeping previous: {}", self._path, e)
                return False
            changed = self._diff_sections(self._config, new_config)
            logger.info("Hot-reload: config.json changed sections: {}", changed or "(none)")

        if self._on_reload:
            try:
                self._on_reload(new_config, agents_changed)
            except Exception:
                logger.exception("Hot-reload: apply callback failed, keeping previous runtime state")
                return False

        self._config = new_config
        if agents_changed and not config_changed:
            logger.info("Hot-reload: agents/*/config.json changed")
        return True


class HotReloadProviderProxy:
    """Wraps an LLMProvider, triggering ConfigReloader before each call.

    Attribute access is delegated to the inner provider, so existing code
    (``provider.generation``, ``hasattr(provider, "chat_stream")``, …) behaves
    exactly as before.  The inner provider is mutated in place on reload, so
    every holder of a reference (AgentLoop, GroupChatEngine, heartbeat, tools)
    sees the update without rewiring.
    """

    def __init__(self, inner: Any, reloader: ConfigReloader):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_reloader", reloader)

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if name == "chat_stream":
            reloader = object.__getattribute__(self, "_reloader")

            async def _stream(*args: Any, **kwargs: Any):
                reloader.maybe_reload()
                async for item in attr(*args, **kwargs):
                    yield item

            return _stream
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_inner"), name, value)

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        object.__getattribute__(self, "_reloader").maybe_reload()
        return await object.__getattribute__(self, "_inner").chat(*args, **kwargs)

    @property
    def inner(self) -> Any:
        return object.__getattribute__(self, "_inner")


def apply_runtime_config(
    new_config: Config,
    provider: Any,
    agent: Any | None = None,
) -> list[str]:
    """Apply hot-reloadable sections of ``new_config`` in place.

    Covers ``providers.*`` (api_key / api_base / extra_headers) and
    ``agents.defaults`` (model / max_tool_iterations / context_window_tokens /
    generation params).  Channels and gateway.port still require a restart.
    Returns a list of human-readable change descriptions.
    """
    from nanobot.providers.base import GenerationSettings

    changes: list[str] = []
    defaults = new_config.agents.defaults
    model = defaults.model

    # ── Provider credentials ──
    p = new_config.get_provider(model)
    api_key = p.api_key if p else None
    api_base = new_config.get_api_base(model)
    extra_headers = p.extra_headers if p else None

    if hasattr(provider, "update_credentials"):
        provider.update_credentials(api_key=api_key, api_base=api_base, extra_headers=extra_headers)
        changes.append("provider credentials")
    else:
        for attr, val in (("api_key", api_key), ("api_base", api_base), ("extra_headers", extra_headers)):
            if val is not None and hasattr(provider, attr):
                if getattr(provider, attr) != val:
                    setattr(provider, attr, val)
                    changes.append(f"provider.{attr}")

    # ── Generation defaults ──
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )

    # ── Agent defaults ──
    if agent is not None:
        if model and getattr(agent, "model", None) != model:
            agent.model = model
            changes.append(f"agent.model → {model}")
        if getattr(agent, "max_iterations", None) != defaults.max_tool_iterations:
            agent.max_iterations = defaults.max_tool_iterations
            changes.append("agent.max_iterations")
        if getattr(agent, "context_window_tokens", None) != defaults.context_window_tokens:
            agent.context_window_tokens = defaults.context_window_tokens
            changes.append("agent.context_window_tokens")
        subagents = getattr(agent, "subagents", None)
        if subagents is not None and getattr(subagents, "model", None) != agent.model:
            subagents.model = agent.model

    return changes


def reload_groupchat_registry(engine: Any, preserved: dict[str, dict] | None = None) -> None:
    """Re-scan ``~/.nanobot/agents`` and refresh the groupchat agent registry.

    ``preserved`` holds runtime-injected entries (e.g. the base model as
    "Nanobot") that must survive the re-scan.
    """
    from nanobot.groupchat.agents import load_agents

    new_registry = load_agents(engine.config, engine.workspace)
    for name, entry in (preserved or {}).items():
        new_registry[name] = entry
        active = getattr(engine, "_active_agents", None)
        if isinstance(active, list) and name not in active:
            active.append(name)
    engine.registry = new_registry
    logger.info("Hot-reload: groupchat registry refreshed ({} agents)", len(new_registry))
