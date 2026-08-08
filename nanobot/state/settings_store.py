"""Single read/write point for nanobot's JSON settings stores.

Consolidates the currently-scattered direct file access to:
  - ``~/.nanobot/providers_models.json``  (providers + their model lists)
  - ``~/.nanobot/agents/<name>/config.json`` (per-agent model/prompt/rank/…)

Every consumer (litellm/httpx routing, telegram callbacks, settings skill CLI,
headless admin) should delegate here so a schema/behaviour change is a ONE-spot
edit instead of N copies. Pure functions — no Telegram/engine dependency, fully
unit-testable.

Backward-compatible and additive: nothing existing moves or changes path.
This module only gives the writes a single home; existing callers may adopt it
gradually.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Agent config fields that are safe to edit via admin (whitelist).
# Base identity fields (name/agent_dir) are derived from the path, not edited.
EDITABLE_AGENT_FIELDS = {
    "model": str,
    "prompt": str,
    "rank": str,
    "reasoning_effort": str,
    "tools_enabled": bool,
}

HOME = Path.home()
_NANOBOT_DIR = HOME / ".nanobot"
PM_FILE = _NANOBOT_DIR / "providers_models.json"
AGENTS_DIR = _NANOBOT_DIR / "agents"


# ── low-level: single read/write, with sanitize on load ────────────────────

def provmodels_path() -> Path:
    return PM_FILE


def load_pm() -> dict[str, Any]:
    """Load providers_models.json, self-healing dirty model lists via sanitize."""
    from nanobot.providers.model_match import sanitize_model_list

    if not PM_FILE.exists():
        return {"providers": {}, "models": {}}
    try:
        data = json.loads(PM_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"providers": {}, "models": {}}
    if not isinstance(data, dict):
        return {"providers": {}, "models": {}}
    data.setdefault("providers", {})
    models = data.setdefault("models", {})
    changed = False
    for prov, mlist in list(models.items()):
        if isinstance(mlist, list):
            clean = sanitize_model_list(mlist)
            if clean != mlist:
                models[prov] = clean
                changed = True
    if changed:
        save_pm(data)  # heal in place
    return data


def save_pm(data: dict[str, Any]) -> None:
    PM_FILE.parent.mkdir(parents=True, exist_ok=True)
    PM_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── provider convenience ────────────────────────────────────────────────────

def list_providers() -> dict[str, dict[str, Any]]:
    return load_pm()["providers"]


def list_models() -> dict[str, list[str]]:
    return load_pm()["models"]


def add_provider(name: str, url: str, api_key: str) -> None:
    pm = load_pm()
    if name in pm["providers"]:
        raise ValueError(f"provider '{name}' already exists (use update_provider)")
    pm["providers"][name] = {"url": url, "apiKey": api_key}
    pm["models"].setdefault(name, [])
    save_pm(pm)


def update_provider(name: str, url: str | None = None, api_key: str | None = None) -> None:
    pm = load_pm()
    if name not in pm["providers"]:
        raise KeyError(f"provider '{name}' not found")
    if url is not None:
        pm["providers"][name]["url"] = url
    if api_key is not None:
        pm["providers"][name]["apiKey"] = api_key
    save_pm(pm)


def delete_provider(name: str) -> None:
    pm = load_pm()
    if name not in pm["providers"]:
        raise KeyError(f"provider '{name}' not found")
    del pm["providers"][name]
    pm["models"].pop(name, None)
    save_pm(pm)


def add_model(provider: str, model: str) -> None:
    pm = load_pm()
    if provider not in pm["providers"]:
        raise KeyError(f"provider '{provider}' not found (add it first)")
    mlist = pm["models"].setdefault(provider, [])
    if model in mlist:
        raise ValueError(f"model '{model}' already exists under '{provider}'")
    mlist.append(model)
    save_pm(pm)


def delete_model(provider: str, model: str) -> None:
    pm = load_pm()
    mlist = pm["models"].get(provider)
    if model in (mlist or []):
        mlist.remove(model)
    else:
        raise KeyError(f"model '{model}' not found under '{provider}'")
    save_pm(pm)


# ── agent convenience ───────────────────────────────────────────────────────

def agents_dir() -> Path:
    return AGENTS_DIR


def agent_config_path(name: str) -> Path:
    return AGENTS_DIR / name.lower() / "config.json"


def load_agent(name: str) -> dict[str, Any]:
    p = agent_config_path(name)
    if not p.exists():
        raise KeyError(f"agent '{name}' has no config ({p})")
    return json.loads(p.read_text(encoding="utf-8"))


def save_agent(name: str, cfg: dict[str, Any]) -> None:
    p = agent_config_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def update_agent(name: str, field: str, value: Any) -> dict[str, Any]:
    """Edit one agent config field (whitelisted). Returns the merged config."""
    if field not in EDITABLE_AGENT_FIELDS:
        raise ValueError(
            f"field '{field}' not editable; allowed: {sorted(EDITABLE_AGENT_FIELDS)}"
        )
    cfg = load_agent(name)
    expected = EDITABLE_AGENT_FIELDS[field]
    if expected is bool and isinstance(value, str):
        value = value.strip().lower() in ("1", "true", "yes", "on", "y")
    elif expected is not str:
        value = expected(value)
    cfg[field] = value
    save_agent(name, cfg)
    return cfg


def create_agent(name: str, model: str, prompt: str | None = None) -> dict[str, Any]:
    """Create a new agent (name-derived path).

    Writes BOTH ``config.json`` (model + prompt) and ``SOUL.md`` (persona).
    ``agent_loader._scan_agents_dir`` only discovers an agent if a persona file
    (SOUL.md / character.json) exists in its dir — a bare config.json is skipped.
    """
    p = agent_config_path(name)
    if p.exists():
        raise ValueError(
            f"agent '{name}' already exists (use update_agent / --edit)"
        )
    cfg = {"model": model}
    if prompt:
        cfg["prompt"] = prompt
    save_agent(name, cfg)
    if prompt:
        (AGENTS_DIR / name.lower() / "SOUL.md").write_text(prompt, encoding="utf-8")
    return cfg


def delete_agent(name: str) -> bool:
    """Delete an agent on disk (its config dir)."""
    d = AGENTS_DIR / name.lower()
    if not d.exists():
        return False
    import shutil

    shutil.rmtree(d)
    return True