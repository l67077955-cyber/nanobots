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
    pid_file = tmp_path / headless.PID_FILE
    pid_file.write_text("999999")
    info = headless.status()
    assert info.running is False
    assert info.pid is None
    assert not pid_file.exists()


def test_spawn_and_stop_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "get_logs_dir", lambda: tmp_path)

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
    assert headless.stop(timeout_s=0.5) is True
    assert killed[0] == (4242, signal.SIGTERM)