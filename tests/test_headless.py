"""Tests for background gateway process management."""

import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nanobot.headless as headless


def test_build_gateway_command_runs_foreground_child():
    cmd = headless.build_gateway_command(
        port=18790,
        workspace="/tmp/ws",
        config="/tmp/config.json",
        verbose=True,
    )
    assert cmd[:5] == [sys.executable, "-m", "nanobot", "gateway", "--foreground"]
    assert "--port" in cmd and "18790" in cmd
    assert "--workspace" in cmd and "/tmp/ws" in cmd
    assert "--config" in cmd and "/tmp/config.json" in cmd
    assert "--verbose" in cmd


def test_status_clears_stale_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(headless, "discover_gateway_pid", lambda: None)
    pid_file = tmp_path / headless.PID_FILE
    pid_file.write_text("999999")
    info = headless.status()
    assert info.running is False
    assert info.pid is None
    assert not pid_file.exists()


def test_status_recovers_missing_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(headless, "discover_gateway_pid", lambda: 4242)
    monkeypatch.setattr(headless, "is_alive", lambda pid: pid == 4242)

    info = headless.status()

    assert info.running is True
    assert info.pid == 4242
    assert (tmp_path / headless.PID_FILE).read_text() == "4242"


def test_gateway_cmdline_detection_accepts_module_and_script_forms():
    assert headless._is_gateway_cmdline([
        "/usr/bin/python3", "-m", "nanobot", "gateway", "--foreground",
    ])
    assert headless._is_gateway_cmdline([
        "/usr/bin/python3", "/root/nanobot-src/nanobot/__main__.py",
        "gateway", "--foreground",
    ])
    assert not headless._is_gateway_cmdline([
        "/usr/bin/python3", "-m", "nanobot", "status",
    ])


def test_spawn_and_stop_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(headless, "discover_gateway_pid", lambda: None)

    child_started = False

    def fake_popen(cmd, **kwargs):
        nonlocal child_started
        child_started = True
        assert "--foreground" in cmd

        class _Proc:
            pid = 4242

        return _Proc()

    with patch("nanobot.headless.subprocess.Popen", side_effect=fake_popen):
        pid = headless.spawn()
    assert pid == 4242
    assert child_started
    assert headless.read_pid() == 4242

    killed = []

    def fake_kill(target_pid, sig):
        killed.append((target_pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)

    def stop_fast(_pid: int) -> bool:
        if killed and killed[-1][1] == signal.SIGTERM:
            return False
        return _pid == 4242

    monkeypatch.setattr(headless, "is_alive", stop_fast)
    # Bypass the /proc cmdline validation (4242 is a fake pid with no
    # /proc entry); trust the recorded PID so stop() sends SIGTERM to it.
    monkeypatch.setattr(headless, "is_gateway_pid", lambda _pid: True)
    assert headless.stop(timeout_s=0.5) is True
    assert killed[0] == (4242, signal.SIGTERM)


def test_stop_timeout_bumped_for_graceful_shutdown():
    """stop() must allow enough time for the gateway's SIGTERM graceful cleanup
    (channels/cron/heartbeat/session teardown) before SIGKILL."""
    assert headless.STOP_TIMEOUT_S >= 15.0


def test_stdout_log_separate_from_gateway_log():
    """stdout/stderr stream must be a separate file from the loguru-owned
    gateway.log so rotation doesn't clash with the spawn-time stdout handle."""
    assert headless.stdout_log_file_path() != headless.log_file_path()
    assert headless.stdout_log_file_path().name == "gateway.stdout.log"
    assert headless.log_file_path().name == "gateway.log"


def test_gateway_registers_sigterm_handler():
    """The gateway foreground branch must convert SIGTERM → KeyboardInterrupt so
    the `finally` cleanup (channels/cron/heartbeat/session) runs on `nanobot
    gateway --stop` instead of being hard-killed by Python's default SIGTERM
    disposition."""
    commands_path = Path(__file__).resolve().parent.parent / "nanobot" / "cli" / "commands.py"
    src = commands_path.read_text()
    assert "SIGTERM" in src, "gateway registers a SIGTERM handler"
    assert "_sigterm_to_keyboard_interrupt" in src, "SIGTERM handler raises KeyboardInterrupt"
    # The detached env var drives the loguru default-sink removal in background mode
    assert "NANOBOT_DETACHED" in src, "gateway checks NANOBOT_DETACHED to drop stderr sink"


def test_spawn_marks_child_detached(monkeypatch, tmp_path):
    """spawn() must set NANOBOT_DETACHED=1 in the child env so the child's
    foreground branch drops loguru's default stderr sink (avoids double writes
    to gateway.log + lets loguru own it with proper rotation)."""
    monkeypatch.setattr(headless, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(headless, "discover_gateway_pid", lambda: None)
    captured_env = {}

    def fake_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env") or {})

        class _Proc:
            pid = 5555

        return _Proc()

    with patch("nanobot.headless.subprocess.Popen", side_effect=fake_popen):
        headless.spawn()
    assert captured_env.get("NANOBOT_DETACHED") == "1", "child env marks detached"
