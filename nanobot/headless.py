"""Headless gateway runtime — detached process management for `nanobot gateway`."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from nanobot.config.paths import get_logs_dir

PID_FILE = "gateway.pid"
STOP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    pid: int | None
    pid_file: Path
    log_file: Path


def pid_file_path() -> Path:
    return get_logs_dir() / PID_FILE


def log_file_path() -> Path:
    return get_logs_dir() / "gateway.log"


def read_pid() -> int | None:
    path = pid_file_path()
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def write_pid(pid: int) -> None:
    path = pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))


def clear_pid() -> None:
    with suppress(OSError):
        pid_file_path().unlink()


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _is_gateway_cmdline(cmdline: list[str]) -> bool:
    joined = " ".join(cmdline)
    return (
        "gateway" in cmdline
        and "--foreground" in cmdline
        and ("nanobot" in joined or "/nanobot/__main__.py" in joined)
    )


def _read_cmdline(pid: int) -> list[str] | None:
    """Read /proc/<pid>/cmdline; return None if unavailable."""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def is_gateway_pid(pid: int) -> bool:
    """Return True only if *pid* is a live process running the gateway command."""
    if not is_alive(pid):
        return False
    cmdline = _read_cmdline(pid)
    if cmdline is None:
        return False
    return _is_gateway_cmdline(cmdline)


def discover_gateway_pid() -> int | None:
    """Best-effort recovery for foreground gateway processes missing a pid file."""
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return None
    own_pid = os.getpid()
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        if not is_gateway_pid(pid):
            continue
        return pid
    return None


def status() -> DaemonStatus:
    pid = read_pid()
    # Validate that the recorded PID actually points at a live gateway process.
    # A stale/corrupt PID file may reference an unrelated surviving process
    # (e.g. a kernel worker thread), which would falsely block spawn()/stop().
    if pid is not None and not is_gateway_pid(pid):
        clear_pid()
        pid = None
    if pid is None:
        pid = discover_gateway_pid()
        if pid is not None:
            write_pid(pid)
    running = bool(pid and is_alive(pid))
    if pid and not running:
        clear_pid()
        pid = None
    return DaemonStatus(
        running=running,
        pid=pid,
        pid_file=pid_file_path(),
        log_file=log_file_path(),
    )


def build_gateway_command(
    *,
    port: int | None = None,
    workspace: str | None = None,
    config: str | None = None,
    verbose: bool = False,
) -> list[str]:
    """Build a foreground gateway command for the detached child process."""
    cmd = [sys.executable, "-m", "nanobot", "gateway", "--foreground"]
    if port is not None:
        cmd.extend(["--port", str(port)])
    if workspace:
        cmd.extend(["--workspace", workspace])
    if config:
        cmd.extend(["--config", config])
    if verbose:
        cmd.append("--verbose")
    return cmd


def spawn(
    *,
    port: int | None = None,
    workspace: str | None = None,
    config: str | None = None,
    verbose: bool = False,
) -> int:
    """Spawn a detached gateway process and return its PID."""
    current = status()
    if current.running and current.pid is not None:
        raise RuntimeError(f"Gateway already running (pid {current.pid})")

    cmd = build_gateway_command(
        port=port,
        workspace=workspace,
        config=config,
        verbose=verbose,
    )
    log_path = log_file_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Spawning background gateway: {} (log={})", cmd, log_path)
    with open(log_path, "a", buffering=1, encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    write_pid(proc.pid)
    return proc.pid


def stop(*, timeout_s: float = STOP_TIMEOUT_S) -> bool:
    """Stop the background gateway. Returns True if a process was stopped."""
    pid = read_pid()
    if not pid:
        return False

    # Guard against a stale PID file pointing at an unrelated process;
    # fall back to discovery so a real foreground gateway can still be found.
    if not is_gateway_pid(pid):
        clear_pid()
        pid = discover_gateway_pid()
        if pid is None:
            return False

    if not is_alive(pid):
        clear_pid()
        return False

    logger.info("Stopping background gateway (pid {})", pid)
    os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not is_alive(pid):
            clear_pid()
            return True
        time.sleep(0.2)

    logger.warning("Gateway did not exit; sending SIGKILL to pid {}", pid)
    with suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    clear_pid()
    return True
