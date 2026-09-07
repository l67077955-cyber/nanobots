"""Agent registry management for GroupChatEngine.

Extracts agent registration logic from GroupChatEngine, providing:
- Agent loading from disk
- CRUD operations on agent configs
- Case-insensitive name resolution
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from loguru import logger


class AgentRegistry:
    """Manages available agent configurations.

    Separates agent management concerns from the GroupChatEngine,
    making it easier to test and reason about agent operations.
    """

    def __init__(self, agents_dir: Path | None = None):
        """Initialize the agent registry.

        Args:
            agents_dir: Directory containing agent configs. Defaults to ~/.nanobot/agents.
        """
        self._agents_dir = agents_dir or (Path.home() / ".nanobot" / "agents")
        self._agents: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """Load all agent configurations from disk."""
        self._agents.clear()
        if not self._agents_dir.exists():
            return

        for agent_dir in self._agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            config_path = agent_dir / "config.json"
            if not config_path.exists():
                continue

            import json
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(cfg, dict):
                    continue
                # Use directory name as canonical agent name
                name = agent_dir.name
                # Preserve agent_dir path for workspace resolution
                cfg["agent_dir"] = str(agent_dir)
                self._agents[name] = cfg
            except Exception as e:
                logger.warning("Failed to load agent {}: {}", agent_dir.name, e)

        logger.info("AgentRegistry: loaded {} agents", len(self._agents))

    def get(self, name: str) -> dict[str, Any] | None:
        """Get an agent's configuration by name (case-insensitive).

        Args:
            name: Agent name (case-insensitive).

        Returns:
            Agent config dict or None if not found.
        """
        canonical = self._resolve_name(name)
        if canonical is None:
            return None
        return self._agents.get(canonical)

    def list(self) -> list[str]:
        """List all agent names."""
        return list(self._agents.keys())

    def _resolve_name(self, name: str) -> str | None:
        """Resolve agent name case-insensitively.

        Args:
            name: Agent name (any case).

        Returns:
            Canonical agent name or None if not found.
        """
        name_lower = name.lower()
        for agent_name in self._agents:
            if agent_name.lower() == name_lower:
                return agent_name
        return None

    def add(self, name: str, config: dict[str, Any]) -> None:
        """Add or update an agent configuration.

        Args:
            name: Agent name.
            config: Agent configuration dict.
        """
        self._agents[name] = config
        self._save_agent(name, config)

    def remove(self, name: str) -> bool:
        """Remove an agent from registry and optionally from disk.

        Args:
            name: Agent name.

        Returns:
            True if agent was removed, False if not found.
        """
        canonical = self._resolve_name(name)
        if canonical is None:
            return False

        del self._agents[canonical]
        return True

    def delete_from_disk(self, name: str) -> bool:
        """Permanently delete an agent's directory from disk.

        Args:
            name: Agent name.

        Returns:
            True if directory was deleted, False otherwise.
        """
        canonical = self._resolve_name(name)
        if canonical is None:
            return False

        agent_dir = self._agents_dir / canonical.lower()
        if not agent_dir.exists():
            return False

        try:
            shutil.rmtree(agent_dir)
            self._agents.pop(canonical, None)
            return True
        except Exception as e:
            logger.warning("Failed to delete agent dir {}: {}", agent_dir, e)
            return False

    def _save_agent(self, name: str, config: dict[str, Any]) -> None:
        """Save agent config to disk."""
        import json
        agent_dir = self._agents_dir / name.lower()
        agent_dir.mkdir(parents=True, exist_ok=True)
        config_path = agent_dir / "config.json"
        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")

    def reload(self) -> None:
        """Reload all agent configurations from disk."""
        self._load()

    def __contains__(self, name: str) -> bool:
        """Check if an agent exists."""
        return self._resolve_name(name) is not None

    def __len__(self) -> int:
        """Return number of registered agents."""
        return len(self._agents)

    def __iter__(self):
        """Iterate over agent names."""
        return iter(self._agents)
