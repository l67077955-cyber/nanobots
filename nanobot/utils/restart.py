"""Restart helpers for nanobot (background restart + notice passing)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_logs_dir


RESTART_NOTIFY_CHANNEL_ENV = "NANOBOT_RESTART_NOTIFY_CHANNEL"
RESTART_NOTIFY_CHAT_ID_ENV = "NANOBOT_RESTART_NOTIFY_CHAT_ID"
RESTART_NOTIFY_METADATA_ENV = "NANOBOT_RESTART_NOTIFY_METADATA"
RESTART_STARTED_AT_ENV = "NANOBOT_RESTART_STARTED_AT"


@dataclass(frozen=True)
class RestartNotice:
    channel: str
    chat_id: str
    started_at_raw: str
    metadata: dict[str, Any] = field(default_factory=dict)


def format_restart_completed_message(started_at_raw: str) -> str:
    """Build restart completion text and include elapsed time when available."""
    elapsed_suffix = ""
    if started_at_raw:
        with suppress(ValueError):
            elapsed_s = max(0.0, time.time() - float(started_at_raw))
            elapsed_suffix = f" in {elapsed_s:.1f}s"
    return f"Restart completed{elapsed_suffix}."


def set_restart_notice_to_env(
    *, channel: str, chat_id: str, metadata: dict[str, Any] | None = None,
) -> None:
    """Write restart notice env values for the next (child) process."""
    os.environ[RESTART_NOTIFY_CHANNEL_ENV] = channel
    os.environ[RESTART_NOTIFY_CHAT_ID_ENV] = chat_id
    os.environ[RESTART_STARTED_AT_ENV] = str(time.time())
    if metadata:
        try:
            os.environ[RESTART_NOTIFY_METADATA_ENV] = json.dumps(metadata, default=str)
        except (TypeError, ValueError):
            os.environ.pop(RESTART_NOTIFY_METADATA_ENV, None)
    else:
        os.environ.pop(RESTART_NOTIFY_METADATA_ENV, None)


def consume_restart_notice_from_env() -> RestartNotice | None:
    """Read and clear restart notice env values once for this process."""
    channel = os.environ.pop(RESTART_NOTIFY_CHANNEL_ENV, "").strip()
    chat_id = os.environ.pop(RESTART_NOTIFY_CHAT_ID_ENV, "").strip()
    started_at_raw = os.environ.pop(RESTART_STARTED_AT_ENV, "").strip()
    metadata_raw = os.environ.pop(RESTART_NOTIFY_METADATA_ENV, "").strip()
    if not (channel and chat_id):
        return None
    metadata: dict[str, Any] = {}
    if metadata_raw:
        try:
            parsed = json.loads(metadata_raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            metadata = parsed
    return RestartNotice(
        channel=channel,
        chat_id=chat_id,
        started_at_raw=started_at_raw,
        metadata=metadata,
    )


def should_show_cli_restart_notice(notice: RestartNotice, session_id: str) -> bool:
    """Return True when a restart notice should be shown in this CLI session."""
    if notice.channel != "cli":
        return False
    if ":" in session_id:
        _, cli_chat_id = session_id.split(":", 1)
    else:
        cli_chat_id = session_id
    return not notice.chat_id or notice.chat_id == cli_chat_id


def is_systemd_service() -> bool:
    """True when running as a systemd service unit."""
    return bool(os.environ.get("INVOCATION_ID"))


def _build_child_command() -> list[str]:
    """Reconstruct a command line that will start an equivalent nanobot process."""
    if not sys.argv:
        return [sys.executable, "-m", "nanobot"]

    argv0 = sys.argv[0] or ""
    # If invoked via console script entrypoint (e.g. `nanobot ...`) or similar non-.py,
    # restart via the reliable -m form so it works after pip install etc.
    if (
        os.path.basename(argv0) in {"nanobot", "nanobot.exe"}
        or not argv0.endswith((".py", ".pyc"))
    ):
        return [sys.executable, "-m", "nanobot"] + sys.argv[1:]

    # Direct python invocation: keep the script path as-is.
    return [sys.executable] + sys.argv[:]


def perform_inplace_restart(*, delay_s: float = 0.0) -> None:
    """Replace the current process in-place (for systemd --foreground services).

    Avoids spawn + exit, so systemd does not wait RestartSec before the new
    instance is running.
    """
    if delay_s > 0:
        time.sleep(delay_s)
    cmd = _build_child_command()
    logger.info("Performing inplace restart: {}", cmd)
    os.execv(cmd[0], cmd)


def perform_background_restart(
    *, delay_s: float = 1.0, extra_env: dict[str, str] | None = None
) -> None:
    """Launch a detached (background) copy of nanobot and terminate the current process.

    This is used for /restart so that:
    - the current "frontend" (attached terminal / command line session) is closed
    - the new instance runs fully in the background (new session, stdio redirected to log)
    - restart notice (if set in env) is passed to the child
    """
    # Give the "Restarting..." reply a chance to be sent over the wire
    if delay_s > 0:
        time.sleep(delay_s)

    cmd = _build_child_command()
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    logs_dir = get_logs_dir()
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    log_path = logs_dir / "nanobot.log"

    logger.info("Performing background restart: {} (log={})", cmd, log_path)

    try:
        # Append mode so we don't truncate previous logs; line buffered
        with open(log_path, "a", buffering=1, encoding="utf-8", errors="replace") as logf:
            subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detach from controlling tty, new process group/session
                close_fds=True,
            )
    except Exception as exc:
        logger.exception("Failed to spawn background nanobot for restart: {}", exc)
        # As a last resort fall back to replacing current process (may keep tty)
        try:
            os.execv(cmd[0], cmd)
        except Exception:
            pass
        # If everything fails, just exit so at least frontend is released
        os._exit(0)

    # Do not run any atexit / finally handlers that might do I/O on closed fds etc.
    os._exit(0)
