"""In-memory process registry for background exec sessions.

Tracks backgrounded shell processes, buffers their stdout/stderr output,
and provides drain/poll semantics for the process management tool.

Inspired by OpenClaw's bash-process-registry.ts.
"""

from __future__ import annotations

import asyncio
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

_ALNUM = string.ascii_lowercase + string.digits
_JOB_TTL_S = 30 * 60  # 30 minutes
_MAX_AGGREGATED = 50_000  # max chars in aggregated output


def _short_id() -> str:
    """Generate a 6-char alphanumeric session ID."""
    return "".join(secrets.choice(_ALNUM) for _ in range(6))


@dataclass
class ProcessSession:
    """A tracked background process."""

    id: str
    command: str
    pid: int | None = None
    started_at: float = field(default_factory=time.time)
    cwd: str | None = None

    # asyncio subprocess handle
    process: Any = None

    # Output buffering
    _pending_stdout: list[str] = field(default_factory=list)
    _pending_stderr: list[str] = field(default_factory=list)
    aggregated: str = ""
    total_output_chars: int = 0

    # State
    exit_code: int | None = None
    exited: bool = False
    backgrounded: bool = True

    # Reader tasks
    _reader_tasks: list[asyncio.Task] = field(default_factory=list)

    @property
    def tail(self) -> str:
        """Last 2000 chars of aggregated output."""
        if len(self.aggregated) <= 2000:
            return self.aggregated
        return self.aggregated[-2000:]

    @property
    def runtime_s(self) -> float:
        return time.time() - self.started_at

    def append_output(self, stream: str, chunk: str) -> None:
        """Append a chunk of output from stdout or stderr."""
        if stream == "stdout":
            self._pending_stdout.append(chunk)
        else:
            self._pending_stderr.append(chunk)
        self.total_output_chars += len(chunk)
        self.aggregated += chunk
        # Cap aggregated output
        if len(self.aggregated) > _MAX_AGGREGATED:
            self.aggregated = self.aggregated[-_MAX_AGGREGATED:]

    def drain(self) -> tuple[str, str]:
        """Drain and return pending stdout and stderr."""
        stdout = "".join(self._pending_stdout)
        stderr = "".join(self._pending_stderr)
        self._pending_stdout.clear()
        self._pending_stderr.clear()
        return stdout, stderr

    def mark_exited(self, exit_code: int | None) -> None:
        self.exited = True
        self.exit_code = exit_code


# ── Global registry ──────────────────────────────────────────

_sessions: dict[str, ProcessSession] = {}
_finished: dict[str, ProcessSession] = {}


def create_session_id() -> str:
    """Generate a unique session ID."""
    for _ in range(100):
        sid = _short_id()
        if sid not in _sessions and sid not in _finished:
            return sid
    return _short_id()


def add_session(session: ProcessSession) -> None:
    """Register a backgrounded process session."""
    _sessions[session.id] = session
    logger.info(
        "process_registry: added session {} (pid={}, cmd={})",
        session.id, session.pid, session.command,
    )


def get_session(session_id: str) -> ProcessSession | None:
    """Get a running session by ID."""
    return _sessions.get(session_id)


def get_finished(session_id: str) -> ProcessSession | None:
    """Get a finished session by ID."""
    return _finished.get(session_id)


def get_any(session_id: str) -> ProcessSession | None:
    """Get any session (running or finished) by ID."""
    return _sessions.get(session_id) or _finished.get(session_id)


def list_all() -> list[ProcessSession]:
    """List all sessions (running + finished), sorted by start time desc."""
    all_sessions = list(_sessions.values()) + list(_finished.values())
    all_sessions.sort(key=lambda s: s.started_at, reverse=True)
    return all_sessions


def move_to_finished(session_id: str) -> None:
    """Move a session from running to finished."""
    session = _sessions.pop(session_id, None)
    if session:
        _finished[session_id] = session
        logger.info(
            "process_registry: session {} finished (exit_code={}, runtime={:.1f}s)",
            session_id, session.exit_code, session.runtime_s,
        )


def remove_session(session_id: str) -> bool:
    """Remove a session from the registry entirely."""
    removed = _sessions.pop(session_id, None) or _finished.pop(session_id, None)
    return removed is not None


def kill_session(session_id: str) -> str:
    """Kill a running session's process."""
    session = _sessions.get(session_id)
    if not session:
        return f"No running session {session_id}"
    if session.exited:
        return f"Session {session_id} already exited"
    if session.process:
        try:
            session.process.kill()
            return f"Killed session {session_id} (pid={session.pid})"
        except Exception as e:
            return f"Error killing session {session_id}: {e}"
    return f"No process handle for session {session_id}"


def prune_expired() -> int:
    """Remove finished sessions older than TTL. Returns count removed."""
    cutoff = time.time() - _JOB_TTL_S
    expired = [sid for sid, s in _finished.items() if s.started_at < cutoff]
    for sid in expired:
        _finished.pop(sid, None)
    return len(expired)


async def _read_stream(session: ProcessSession, stream_name: str, stream: Any) -> None:
    """Background task to read a subprocess stream into the session buffer."""
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            session.append_output(stream_name, text)
    except Exception as e:
        logger.debug("process_registry: _read_stream error for {}: {}", session.id, e)


async def start_background_readers(session: ProcessSession) -> None:
    """Start background tasks to read stdout/stderr from the process."""
    if session.process is None:
        return
    if session.process.stdout:
        task = asyncio.create_task(
            _read_stream(session, "stdout", session.process.stdout),
            name=f"bg-stdout-{session.id}",
        )
        session._reader_tasks.append(task)
    if session.process.stderr:
        task = asyncio.create_task(
            _read_stream(session, "stderr", session.process.stderr),
            name=f"bg-stderr-{session.id}",
        )
        session._reader_tasks.append(task)

    # Also start a waiter that marks the session as exited
    async def _wait_exit():
        try:
            exit_code = await session.process.wait()
            session.mark_exited(exit_code)
            move_to_finished(session.id)
        except Exception as e:
            logger.debug("process_registry: _wait_exit error for {}: {}", session.id, e)
            session.mark_exited(-1)
            move_to_finished(session.id)

    session._reader_tasks.append(
        asyncio.create_task(_wait_exit(), name=f"bg-wait-{session.id}")
    )
