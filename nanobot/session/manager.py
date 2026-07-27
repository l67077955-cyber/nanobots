"""Session management for conversation history."""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir
from nanobot.utils.helpers import ensure_dir, safe_filename


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    Consolidation replaces old messages with summaries in-place to preserve
    cache-friendly prefix stability.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0
    # Cache for get_history result
    _history_cache: tuple[int, int, list[dict[str, Any]]] | None = field(
        default=None, repr=False, compare=False
    )  # (last_consolidated, msg_count, result)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _find_legal_start(messages: list[dict[str, Any]]) -> int:
        """Find first index where every tool result has a matching assistant tool_call."""
        declared: set[str] = set()
        start = 0
        for i, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    start = i + 1
                    declared.clear()
                    for prev in messages[start:i + 1]:
                        if prev.get("role") == "assistant":
                            for tc in prev.get("tool_calls") or []:
                                if isinstance(tc, dict) and tc.get("id"):
                                    declared.add(str(tc["id"]))
        return start

    def get_history(self, max_messages: int | None = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a legal tool-call boundary.
        
        Results are cached based on (last_consolidated, msg_count) to avoid
        redundant computation when no new messages have been added.
        """
        msg_count = len(self.messages)
        # Check cache - valid if last_consolidated and msg_count unchanged
        if self._history_cache is not None:
            cached_lc, cached_count, cached_result = self._history_cache
            if cached_lc == self.last_consolidated and cached_count == msg_count:
                return cached_result

        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated if max_messages is None else unconsolidated[-max_messages:] if max_messages > 0 else []

        # Drop leading non-user messages to avoid starting mid-turn when possible.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                sliced = sliced[i:]
                break

        # Some providers reject orphan tool results if the matching assistant
        # tool_calls message fell outside the fixed-size history window.
        start = self._find_legal_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            entry: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
            for key in ("tool_calls", "tool_call_id", "name"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        # Cache the result
        self._history_cache = (self.last_consolidated, msg_count, out)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()
        self._history_cache = None  # Invalidate cache


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    New messages are appended incrementally (O(1) per message).
    Full rewrite only happens during consolidation.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}
        # Track the number of messages on disk per session so append_message()
        # knows where the in-memory messages diverge from the file.
        self._disk_msg_count: dict[str, int] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    async def get_or_create_async(self, key: str) -> Session:
        """Async version of get_or_create — loads from disk without blocking the event loop."""
        if key in self._cache:
            return self._cache[key]

        session = await self._load_async(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    async def _load_async(self, key: str) -> Session | None:
        """Async wrapper for _load — runs sync I/O in a thread to avoid blocking the event loop."""
        return await asyncio.to_thread(self._load, key)

    def _load(self, key: str) -> Session | None:
        """Load a session from disk (sync I/O — prefer _load_async in async contexts)."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            session = Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
            self._disk_msg_count[key] = len(messages)
            return session
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    async def save_async(self, session: Session) -> None:
        """Async wrapper for save — runs sync I/O in a thread to avoid blocking the event loop."""
        await asyncio.to_thread(self.save, session)

    async def append_message_async(self, session: Session) -> None:
        """Async wrapper for append_message — runs sync I/O in a thread."""
        await asyncio.to_thread(self.append_message, session)

    def append_message(self, session: Session) -> None:
        """Append only the new messages (since last save) to the JSONL file.

        O(1) per new message — avoids full-file rewrite for the common case
        where only a few messages were added since the last save.

        Also updates the metadata line with new updated_at / last_consolidated
        by rewriting just the first line (cheap for small metadata).
        """
        path = self._get_session_path(session.key)
        if not path.exists():
            # File doesn't exist yet — fall through to full save
            self.save(session)
            return

        disk_count = self._disk_msg_count.get(session.key, 0)
        new_msgs = session.messages[disk_count:]

        if not new_msgs and disk_count == len(session.messages):
            # No new messages — just update metadata if needed
            self._rewrite_metadata(session, path)
            return

        # Append new message lines
        with open(path, "a", encoding="utf-8") as f:
            for msg in new_msgs:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        # Update metadata (rewrite just the first line)
        self._rewrite_metadata(session, path)

        self._disk_msg_count[session.key] = len(session.messages)
        self._cache[session.key] = session

    def _rewrite_metadata(self, session: Session, path: Path) -> None:
        """Rewrite only the first (metadata) line of the session file.

        This is O(1) for the first line; avoids a full-file rewrite just
        to update timestamps/last_consolidated.
        """
        try:
            # Read all lines
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()

            if not lines:
                return

            new_meta = json.dumps({
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }, ensure_ascii=False) + "\n"

            lines[0] = new_meta

            # Atomic write
            tmp_path = path.with_suffix(".jsonl.tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(tmp_path, path)
            except BaseException:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        except Exception:
            logger.debug("Failed to rewrite metadata for session {}: non-critical", session.key)

    def save(self, session: Session) -> None:
        """Save a session to disk (full rewrite — prefer append_message for incremental saves).

        Uses atomic write (temp file + os.replace) to prevent corruption on crash.
        Call this after consolidation or when the file doesn't exist yet.
        """
        path = self._get_session_path(session.key)

        # Atomic write: write to temp file first, then replace to avoid corruption
        tmp_path = path.with_suffix(".jsonl.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                metadata_line = {
                    "_type": "metadata",
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)
        except BaseException:
            # Clean up temp file on failure
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        self._disk_msg_count[session.key] = len(session.messages)
        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)
        self._disk_msg_count.pop(key, None)

    async def list_sessions_async(self) -> list[dict[str, Any]]:
        """Async wrapper for list_sessions — runs sync I/O in a thread."""
        return await asyncio.to_thread(self.list_sessions)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions (sync I/O — prefer list_sessions_async in async contexts).

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key")
                            if not key:
                                # Legacy file without key in metadata — fallback with warning
                                key = path.stem.replace("_", ":", 1)
                                logger.warning(
                                    "Session file {} has no key in metadata, "
                                    "fallback to filename (may be inaccurate if chat_id contains '_')",
                                    path.name,
                                )
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                logger.warning("Failed to read session metadata from {}", path.name, exc_info=True)
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
