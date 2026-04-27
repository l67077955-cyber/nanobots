"""State persistence for group chat engine.

Manages all file I/O for group chat state:
- Active agents list
- Leader selection
- Named agent groups
- Session event logging
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.utils.helpers import cn_now as _cn_now


_NANOBOT_DIR = Path.home() / ".nanobot"


class GroupChatState:
    """Unified persistence layer for group chat state.

    All state files live under ``~/.nanobot/``:
    - ``active_agents.json``  — ordered list of active agent names
    - ``leader.txt``          — current leader name (or absent)
    - ``groups.json``         — saved named groups
    - ``collab-sessions/``    — per-session event logs
    """

    def __init__(self, registry: dict[str, Any]) -> None:
        self._registry = registry
        self._session_dir: Path | None = None

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

    # ── Session Events ───────────────────────────────────────

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @session_dir.setter
    def session_dir(self, value: Path | None) -> None:
        self._session_dir = value

    def create_session(self) -> Path:
        """Create a new session directory and return its path."""
        timestamp = _cn_now().strftime("%Y%m%d-%H%M%S")
        sessions_dir = _NANOBOT_DIR / "collab-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = sessions_dir / f"gc-{timestamp}"
        self._session_dir.mkdir(parents=True, exist_ok=True)
        return self._session_dir

    def save_event(
        self,
        event_type: str,
        *,
        agent: str = "",
        content: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append a structured event to session.jsonl.

        Event types: session_start, round_start, round_end, message,
                     tool_call, tool_result, agent_comm, system.
        """
        if not self._session_dir:
            return
        record: dict[str, Any] = {
            "type": event_type,
            "ts": _cn_now().isoformat(),
        }
        if agent:
            record["agent"] = agent
        if content:
            record["content"] = content
        if extra:
            record.update(extra)
        try:
            with open(self._session_dir / "session.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.debug("save_event failed: {}", e)

    def save_round_summary(
        self,
        round_num: int,
        agents_responded: int,
        comm_count: int = 0,
        duration: float = 0.0,
    ) -> None:
        """Write a round_end event summarizing the round."""
        self.save_event("round_end", extra={
            "round": round_num,
            "agents_responded": agents_responded,
            "comm_count": comm_count,
            "duration": round(duration, 2),
        })

    def save_message(self, sender: str, content: str, history: list[dict[str, str]]) -> None:
        """Log a message to session chat_log.txt and session.jsonl."""
        if self._session_dir:
            with open(self._session_dir / "chat_log.txt", "a") as f:
                f.write(f"[{sender}]: {content}\n---\n")
        self.save_event("message", agent=sender, content=content)
