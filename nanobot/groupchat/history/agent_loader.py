"""Agent configuration loader for group chat.

Loads agent personas from:
1. Config-defined agents (inline persona or file path)
2. Directory scan (agents/<name>/workspace/SOUL.md or character.json)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.groupchat.config import GroupChatConfig


def load_agents(config: GroupChatConfig, workspace: Path) -> dict[str, dict[str, Any]]:
    """Load agent configurations from config and/or agents directory.

    Returns:
        Dict of agent_name -> {"model": str, "prompt": str}
    """
    agents: dict[str, dict[str, Any]] = {}
    excluded = {name.lower() for name in config.excluded_agents}

    # 1. Load from config.agents (explicit definitions)
    for name, agent_cfg in config.agents.items():
        if name.lower() in excluded:
            logger.info("Groupchat: skipping excluded agent {}", name)
            continue

        prompt = _resolve_persona(agent_cfg.persona, agent_cfg.character_json, workspace)
        if prompt:
            agents[name] = {"model": agent_cfg.model, "prompt": prompt}
            logger.info("Groupchat: loaded agent {} (model={})", name, agent_cfg.model)

    # 2. Scan agents_dir for auto-discovery
    if config.agents_dir:
        agents_dir = Path(config.agents_dir).expanduser()
        if not agents_dir.is_absolute():
            agents_dir = workspace / config.agents_dir
        if agents_dir.exists():
            _scan_agents_dir(agents_dir, agents, excluded, config)

    if not agents:
        logger.warning("Groupchat: no agents loaded!")

    return agents


def _resolve_persona(persona_path: str, character_json: str, workspace: Path) -> str | None:
    """Resolve persona text from path or inline content."""
    if not persona_path:
        if character_json:
            return _load_character_json(Path(character_json).expanduser(), workspace)
        return None

    # Check if it's inline text (not a path)
    if "\n" in persona_path or len(persona_path) > 200:
        return persona_path

    # Resolve as file path
    path = Path(persona_path).expanduser()
    if not path.is_absolute():
        path = workspace / persona_path
    if path.exists():
        return path.read_text().strip()

    logger.warning("Groupchat: persona file not found: {}", path)
    return None


def _load_character_json(path: Path, workspace: Path) -> str | None:
    """Load SillyTavern character.json as persona text."""
    if not path.is_absolute():
        path = workspace / path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # Try nanobot's built-in parser first
        try:
            from nanobot.sillytavern.character_card import (
                build_character_prompt,
                parse_character_card,
            )
            parsed, err, spec = parse_character_card(path.read_text())
            if parsed and not err:
                return build_character_prompt(parsed, include_post_history=True)
        except ImportError:
            pass
        # Fallback: extract description/personality directly
        parts = []
        for key in ("description", "personality", "scenario", "first_mes"):
            if data.get(key):
                parts.append(data[key])
        return "\n\n".join(parts) if parts else None
    except Exception as e:
        logger.warning("Groupchat: failed to load character.json {}: {}", path, e)
        return None


def _scan_agents_dir(
    agents_dir: Path,
    agents: dict[str, dict[str, Any]],
    excluded: set[str],
    config: GroupChatConfig,
) -> None:
    """Scan agents directory for auto-discovery of agents."""
    global_hyperparams = {}
    defaults_file = agents_dir / "_defaults.json"
    if defaults_file.exists():
        try:
            global_hyperparams = json.loads(defaults_file.read_text()).get("hyperparams", {})
        except Exception:
            pass

    for d in sorted(agents_dir.iterdir()):
        if not d.is_dir():
            continue

        name = d.name.capitalize()
        if d.name.lower() in excluded or name.lower() in excluded:
            logger.info("Groupchat: skipping excluded agent {}", name)
            continue
        if name in agents:
            continue  # Already loaded from explicit config

        # Check for system agent (role: system in config.json)
        config_file = d / "config.json"
        if config_file.exists():
            try:
                _cfg = json.loads(config_file.read_text())
                if _cfg.get("role") == "system":
                    model = _cfg.get("model", "?")
                    desc = _cfg.get("description", "系统 agent")
                    tools_enabled = _cfg.get("tools_enabled", False)
                    agent_data = {
                        "model": model, "prompt": "", "role": "system",
                        "description": desc, "tools_enabled": tools_enabled,
                        "workspace_scope": "workspace", "agent_dir": str(d),
                    }
                    if isinstance(_cfg.get("tools"), dict):
                        agent_data["tools"] = _cfg["tools"]
                    
                    hyperparams = _cfg.get("hyperparams")
                    if _cfg.get("reasoning_effort"):
                        hyperparams = hyperparams or {}
                        hyperparams.setdefault("reasoning_effort", _cfg["reasoning_effort"])
                    if hyperparams:
                        agent_data["hyperparams"] = hyperparams

                    agents[name] = agent_data
                    logger.info("Groupchat: discovered system agent {} (model={}, {})", name, model, desc)
                    continue
            except Exception:
                pass

        ws = d / "workspace"
        soul_file = ws / "SOUL.md" if ws.exists() else d / "SOUL.md"
        prompt = soul_file.read_text().strip() if soul_file.exists() else _load_character_json(d / "character.json", agents_dir)

        if not prompt:
            continue

        # Read model from agent's config.json
        model = "minimax/minimax-m2.5"  # default
        config_file = d / "config.json"
        tools_cfg = None  # Will be dict or None
        tools_enabled = False
        workspace_scope = "workspace"  # default
        hyperparams = None  # Per-agent sampling overrides
        if config_file.exists():
            try:
                acfg = json.loads(config_file.read_text())
                # Top-level 'model' takes priority (written by /editagent)
                model = acfg.get("model", model)
                # Granular tools config: {web_search: true, exec: false, ...}
                if isinstance(acfg.get("tools"), dict):
                    tools_cfg = acfg["tools"]
                # Legacy: tools_enabled: true/false
                tools_enabled = acfg.get("tools_enabled", False)
                # Per-agent workspace scope
                workspace_scope = acfg.get("workspace", "workspace")
                # Per-agent hyperparams (temperature, top_p, max_tokens, reasoning_effort, etc.)
                hyperparams = acfg.get("hyperparams") or (dict(global_hyperparams) if global_hyperparams else None)
                if acfg.get("reasoning_effort"):
                    hyperparams = hyperparams or {}
                    hyperparams.setdefault("reasoning_effort", acfg["reasoning_effort"])
            except Exception:
                pass

        # Special name handling
        if d.name == "grok":
            name = "Grok"

        agent_data: dict[str, Any] = {"model": model, "prompt": prompt, "tools_enabled": tools_enabled}
        if tools_cfg is not None:
            agent_data["tools"] = tools_cfg
        if hyperparams:
            agent_data["hyperparams"] = hyperparams

        # Per-agent workspace scope and agent directory
        agent_data["workspace_scope"] = workspace_scope
        agent_data["agent_dir"] = str(d)

        for key, filename in [("examples", "EXAMPLES.md"), ("instructions", "INSTRUCTIONS.md")]:
            f = ws / filename if ws.exists() else d / filename
            if f.exists():
                agent_data[key] = f.read_text().strip()
                logger.info("Groupchat: loaded {} for {}", filename, name)

        agents[name] = agent_data
        logger.info("Groupchat: discovered agent {} (model={})", name, model)


def persist_agent_file(
    agent_name: str,
    filename: str,
    content: str,
    agents_dir: Path,
) -> None:
    """Write content to the agent's workspace file."""
    if not agents_dir.is_dir():
        logger.warning("Agents dir not found: {}", agents_dir)
        return
    for d in agents_dir.iterdir():
        if d.is_dir() and d.name.lower() == agent_name.lower():
            ws = d / "workspace"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / filename).write_text(content)
            logger.info("Persisted {} for agent {} ({} chars)", filename, agent_name, len(content))
            return
    logger.warning("Could not find agent dir for {}", agent_name)
