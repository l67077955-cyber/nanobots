"""State persistence for group chat engine.

Manages all file I/O for group chat state:
- Active agents list
- Leader selection
- Chat mode (serial/broadcast)
- Named agent groups
- Message sync to state.yaml

注意：不再写 session.jsonl。所有事件记录都在 state.yaml 中。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger


_NANOBOT_DIR = Path.home() / ".nanobot"


class GroupChatState:
    """Unified persistence layer for group chat state.

    All state files live under ``~/.nanobot/``:
    - ``active_agents.json``  — ordered list of active agent names
    - ``leader.txt``          — current leader name (or absent)
    - ``chat_mode.txt``       — "serial" | "broadcast"
    - ``groups.json``         — saved named groups
    """

    def __init__(self, registry: dict[str, Any], default_mode: str = "serial") -> None:
        self._registry = registry
        self._default_mode = default_mode
        self._session_dir: Path | None = None
        self.state_bus: Any | None = None  # FileStateBus reference

    # ── Active Agents ────────────────────────────────────────

    @property
    def _active_file(self) -> Path:
        return _NANOBOT_DIR / "active_agents.json"

    def save_active(self, agents: list[str]) -> None:
        """Persist active agents list (and order) to disk."""
        try:
            self._active_file.parent.mkdir(parents=True, exist_ok=True)
            self._active_file.write_text(json.dumps(agents, ensure_ascii=False))
        except Exception:
            pass

    def load_active(self) -> list[str]:
        """Load active agents from disk, filtering out unregistered ones."""
        if self._active_file.exists():
            try:
                saved = json.loads(self._active_file.read_text())
                valid = [a for a in saved if a in self._registry]
                if valid:
                    logger.info("Restored active agents: {}", valid)
                return valid
            except Exception:
                pass
        return []

    # ── Leader ───────────────────────────────────────────────

    def save_leader(self, leader: str | None) -> None:
        try:
            p = _NANOBOT_DIR / "leader.txt"
            if leader:
                p.write_text(leader)
            elif p.exists():
                p.unlink()
        except Exception:
            pass

    def load_leader(self) -> str | None:
        p = _NANOBOT_DIR / "leader.txt"
        if p.exists():
            try:
                name = p.read_text().strip()
                if name and name in self._registry:
                    logger.info("Restored leader: {}", name)
                    return name
            except Exception:
                pass
        return None

    # ── Mode ─────────────────────────────────────────────────

    def save_mode(self, mode: str) -> None:
        try:
            p = _NANOBOT_DIR / "chat_mode.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(mode)
        except Exception:
            pass

    def load_mode(self) -> str:
        p = _NANOBOT_DIR / "chat_mode.txt"
        if p.exists():
            try:
                mode = p.read_text().strip()
                if mode in ("serial", "broadcast"):
                    logger.info("Restored chat mode: {}", mode)
                    return mode
            except Exception:
                pass
        return self._default_mode

    # ── Groups ───────────────────────────────────────────────

    @property
    def _groups_file(self) -> Path:
        return _NANOBOT_DIR / "groups.json"

    def load_groups(self) -> dict[str, list[str]]:
        if self._groups_file.exists():
            try:
                return json.loads(self._groups_file.read_text())
            except Exception:
                return {}
        return {}

    def save_groups(self, groups: dict[str, list[str]]) -> None:
        self._groups_file.parent.mkdir(parents=True, exist_ok=True)
        self._groups_file.write_text(json.dumps(groups, ensure_ascii=False, indent=2))

    # ── Session ──────────────────────────────────────────────

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @session_dir.setter
    def session_dir(self, value: Path | None) -> None:
        self._session_dir = value

    def save_message(self, sender: str, content: str, history: list[dict[str, str]]) -> None:
        """Sync a message to state.yaml conversation chain."""
        if self.state_bus:
            try:
                self.state_bus.append_conversation(sender, content)
            except Exception:
                pass
