"""Tests for nanobot.config.hotreload — on-demand config hot reload."""

import asyncio
import json
import os
import time
from pathlib import Path

from nanobot.config.hotreload import (
    ConfigReloader,
    HotReloadProviderProxy,
    apply_runtime_config,
)
from nanobot.config.schema import Config


def _write_config(path: Path, **overrides) -> None:
    data = {
        "agents": {"defaults": {"model": "openrouter/z-ai/glm-5-turbo"}},
        "providers": {"openrouter": {"apiKey": "sk-old", "apiBase": "https://openrouter.ai/api/v1"}},
    }
    for k, v in overrides.items():
        data[k] = v
    path.write_text(json.dumps(data))


def _touch(path: Path, content: str) -> None:
    """Write content and bump mtime to guarantee a visible change."""
    path.write_text(content)
    bumped = time.time() + 1.5
    os.utime(path, (bumped, bumped))


class DummyProvider:
    """Minimal stand-in for an LLMProvider."""

    def __init__(self):
        self.api_key = "sk-old"
        self.api_base = "https://openrouter.ai/api/v1"
        self.generation = None
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return "ok"


class DummyAgent:
    def __init__(self):
        self.model = "openrouter/z-ai/glm-5-turbo"
        self.max_iterations = 40
        self.context_window_tokens = 200_000


def test_no_change_no_reload(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path)
    config = Config.model_validate(json.loads(cfg_path.read_text()))

    applied = []
    r = ConfigReloader(config, config_path=cfg_path, on_reload=lambda c, a: applied.append(c))
    assert r.maybe_reload() is False
    assert applied == []


def test_config_change_triggers_reload(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path)
    config = Config.model_validate(json.loads(cfg_path.read_text()))

    applied = []
    r = ConfigReloader(config, config_path=cfg_path, on_reload=lambda c, a: applied.append((c, a)))

    _touch(cfg_path, json.dumps({
        "agents": {"defaults": {"model": "openrouter/z-ai/glm-5-turbo"}},
        "providers": {"openrouter": {"apiKey": "sk-NEW", "apiBase": "https://openrouter.ai/api/v1"}},
    }))

    assert r.maybe_reload() is True
    assert len(applied) == 1
    assert applied[0][0].providers.openrouter.api_key == "sk-NEW"
    # Second call with no further change → no reload
    assert r.maybe_reload() is False


def test_broken_json_keeps_previous_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path)
    config = Config.model_validate(json.loads(cfg_path.read_text()))

    applied = []
    r = ConfigReloader(config, config_path=cfg_path, on_reload=lambda c, a: applied.append(c))

    _touch(cfg_path, "{ not valid json !!!")
    assert r.maybe_reload() is False
    assert applied == []
    assert r.config.providers.openrouter.api_key == "sk-old"

    # Fixing the file afterwards reloads again (retry on next mtime change)
    _write_config(cfg_path)
    bumped = time.time() + 3
    os.utime(cfg_path, (bumped, bumped))
    assert r.maybe_reload() is True


def test_apply_runtime_config_updates_provider_and_agent(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path)
    _touch(cfg_path, json.dumps({
        "agents": {"defaults": {"model": "openrouter/z-ai/glm-5-turbo", "max_tool_iterations": 55}},
        "providers": {"openrouter": {"apiKey": "sk-NEW", "apiBase": "https://or.example.com/v1"}},
    }))
    new_config = Config.model_validate(json.loads(cfg_path.read_text()))

    provider = DummyProvider()
    agent = DummyAgent()
    changes = apply_runtime_config(new_config, provider, agent=agent)

    assert provider.api_key == "sk-NEW"
    assert provider.api_base == "https://or.example.com/v1"
    assert provider.generation is not None
    assert agent.max_iterations == 55
    assert changes


def test_agents_dir_change_sets_flag(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path)
    agents_dir = tmp_path / "agents"
    (agents_dir / "kirk").mkdir(parents=True)
    agent_cfg = agents_dir / "kirk" / "config.json"
    agent_cfg.write_text(json.dumps({"model": "a/b"}))

    config = Config.model_validate(json.loads(cfg_path.read_text()))
    flags = []
    r = ConfigReloader(config, config_path=cfg_path,
                       on_reload=lambda c, a: flags.append(a), agents_dir=agents_dir)
    assert r.maybe_reload() is False

    _touch(agent_cfg, json.dumps({"model": "c/d"}))
    assert r.maybe_reload() is True
    assert flags == [True]


def test_provider_proxy_triggers_reloader(tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_config(cfg_path)
    config = Config.model_validate(json.loads(cfg_path.read_text()))
    r = ConfigReloader(config, config_path=cfg_path)

    provider = DummyProvider()
    proxy = HotReloadProviderProxy(provider, r)

    # attribute delegation
    assert proxy.api_key == "sk-old"
    proxy.api_key = "sk-x"
    assert provider.api_key == "sk-x"

    # chat triggers maybe_reload (no change → still one underlying call)
    out = asyncio.run(proxy.chat([{"role": "user", "content": "hi"}]))
    assert out == "ok"
    assert provider.calls == 1

    # streaming attr absent on inner → absent on proxy
    assert not hasattr(proxy, "chat_stream")
