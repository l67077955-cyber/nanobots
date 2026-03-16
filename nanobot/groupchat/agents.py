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
    for d in sorted(agents_dir.iterdir()):
        if not d.is_dir():
            continue

        name = d.name.capitalize()
        if d.name.lower() in excluded or name.lower() in excluded:
            logger.info("Groupchat: skipping excluded agent {}", name)
            continue
        if name in agents:
            continue  # Already loaded from explicit config

        # Look for SOUL.md
        soul_file = d / "workspace" / "SOUL.md"
        if not soul_file.exists():
            soul_file = d / "SOUL.md"

        prompt = None
        if soul_file.exists():
            prompt = soul_file.read_text().strip()

        # Fallback to character.json
        if not prompt:
            char_file = d / "character.json"
            if char_file.exists():
                prompt = _load_character_json(char_file, agents_dir)

        if not prompt:
            continue

        # Read model from agent's config.json
        model = "minimax/minimax-m2.5"  # default
        config_file = d / "config.json"
        tools_cfg = None  # Will be dict or None
        tools_enabled = False
        if config_file.exists():
            try:
                acfg = json.loads(config_file.read_text())
                # Top-level 'model' takes priority (written by /editagent)
                # Fall back to agents.defaults.model for compat
                model = acfg.get("model") or acfg.get("agents", {}).get("defaults", {}).get("model", model)
                # Granular tools config: {web_search: true, exec: false, ...}
                if isinstance(acfg.get("tools"), dict):
                    tools_cfg = acfg["tools"]
                # Legacy: tools_enabled: true/false
                tools_enabled = acfg.get("tools_enabled", False)
            except Exception:
                pass

        # Special name handling
        if d.name == "grok":
            name = "Grok"

        agent_data: dict[str, Any] = {"model": model, "prompt": prompt, "tools_enabled": tools_enabled}
        if tools_cfg is not None:
            agent_data["tools"] = tools_cfg

        # Load optional EXAMPLES.md (few-shot dialogue examples)
        ws = d / "workspace"
        examples_file = ws / "EXAMPLES.md" if ws.exists() else d / "EXAMPLES.md"
        if examples_file.exists():
            agent_data["examples"] = examples_file.read_text().strip()
            logger.info("Groupchat: loaded EXAMPLES.md for {}", name)

        # Load optional INSTRUCTIONS.md (post-history reinforcement)
        instr_file = ws / "INSTRUCTIONS.md" if ws.exists() else d / "INSTRUCTIONS.md"
        if instr_file.exists():
            agent_data["instructions"] = instr_file.read_text().strip()
            logger.info("Groupchat: loaded INSTRUCTIONS.md for {}", name)

        agents[name] = agent_data
        logger.info("Groupchat: discovered agent {} (model={})", name, model)
