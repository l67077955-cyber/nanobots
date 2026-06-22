#!/usr/bin/env python3
"""Headless task injection + inbound dispatch smoke test."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets

from nanobot.config.paths import get_inbox_dir, get_logs_dir

WS_URL = "ws://127.0.0.1:18791/?token=nanobot-watch-2026&chat_id=dispatch-test"
ROOM_EVENTS = get_logs_dir() / "room_events.jsonl"
GATEWAY_LOG = get_logs_dir() / "gateway.log"
POLL_TIMEOUT = 12.0


def _tail_lines(path: Path, n: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _grep_room(marker: str) -> list[dict]:
    hits = []
    for line in _tail_lines(ROOM_EVENTS, 200):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") != "user_input":
            continue
        content = str(row.get("content", ""))
        if marker in content:
            hits.append(row)
    return hits


def _grep_log(marker: str) -> bool:
    return any(marker in ln for ln in _tail_lines(GATEWAY_LOG, 120))


async def ws_send(content: str) -> list[dict]:
    received: list[dict] = []
    async with websockets.connect(WS_URL, open_timeout=5) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5)
        await ws.send(json.dumps({"type": "message", "content": content}))
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                received.append(json.loads(raw))
            except asyncio.TimeoutError:
                break
    return received


def run_pytest() -> bool:
    cmd = [
        sys.executable, "-m", "pytest",
        "tests/test_dispatch.py",
        "tests/test_inject_routing.py",
        "-q",
        "--tb=line",
        "-k", "not test_channel_manager_passes_media_to_inject",
    ]
    proc = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    print(proc.stdout.strip())
    if proc.returncode != 0 and proc.stderr:
        print(proc.stderr.strip()[-500:])
    return proc.returncode == 0


async def live_dispatch_test() -> dict:
    results: dict = {"errors": []}
    marker_ws = f"DISPATCH_WS_{uuid.uuid4().hex[:8]}"
    marker_inbox = f"DISPATCH_INBOX_{uuid.uuid4().hex[:8]}"
    inbox_file = get_inbox_dir() / f"smoke_{marker_inbox}.txt"

    # Stop any running turn first
    await ws_send("/stop")
    await asyncio.sleep(0.5)

    # ── 1. Slash command: consumed by dispatcher, not injected ──
    before_lines = len(_tail_lines(ROOM_EVENTS))
    await ws_send("/agents")
    await asyncio.sleep(1.0)
    slash_injected = any(
        "/agents" in str(json.loads(ln).get("content", ""))
        for ln in _tail_lines(ROOM_EVENTS, 30)
        if '"kind": "user_input"' in ln
    )
    # room_events may lag; check last few new lines only
    new_lines = _tail_lines(ROOM_EVENTS)[before_lines:]
    slash_hit_inject = any(
        "/agents" in str(json.loads(ln).get("content", ""))
        for ln in new_lines
        if '"kind": "user_input"' in ln
    )
    results["slash_not_injected"] = not slash_hit_inject

    # ── 2. Plain text → bus → engine.inject ──
    await ws_send(marker_ws)
    t0 = time.monotonic()
    ws_hits: list[dict] = []
    while time.monotonic() - t0 < POLL_TIMEOUT:
        ws_hits = _grep_room(marker_ws)
        if ws_hits:
            break
        await asyncio.sleep(0.5)
    results["ws_injected"] = bool(ws_hits)
    if ws_hits:
        results["ws_active_agents"] = ws_hits[-1].get("extra", {}).get("active_agents")
        results["ws_room"] = ws_hits[-1].get("room_id")

    # ── 3. Inbox file-drop → engine.inject ──
    inbox_file.write_text(f"无头分发测试 {marker_inbox}\n请回复 OK。", encoding="utf-8")
    t1 = time.monotonic()
    inbox_archived = False
    inbox_log = False
    inbox_hits: list[dict] = []
    while time.monotonic() - t1 < POLL_TIMEOUT:
        inbox_log = _grep_log(f"Inbox: inject {inbox_file.name}")
        inbox_archived = not inbox_file.exists()
        inbox_hits = _grep_room(marker_inbox)
        if inbox_log and inbox_archived and inbox_hits:
            break
        await asyncio.sleep(0.5)
    results["inbox_injected"] = bool(inbox_hits)
    results["inbox_archived"] = inbox_archived
    results["inbox_log"] = inbox_log
    if inbox_hits:
        results["inbox_active_agents"] = inbox_hits[-1].get("extra", {}).get("active_agents")
        results["inbox_content_prefix"] = inbox_hits[-1].get("content", "")[:40]

    await ws_send("/stop")
    return results


async def main() -> int:
    print("=== Headless dispatch + task injection test ===\n")

    print("[1] Unit tests (dispatch + inject routing)")
    unit_ok = run_pytest()
    print(f"  unit OK: {unit_ok}\n")

    print("[2] Live gateway (WS + inbox)")
    try:
        live = await live_dispatch_test()
    except Exception as exc:
        print(f"  LIVE ERROR: {exc}")
        return 1

    print(f"  slash /agents not injected: {live.get('slash_not_injected')}")
    print(f"  WS plain text injected: {live.get('ws_injected')}")
    if live.get("ws_active_agents"):
        print(f"    active_agents: {live['ws_active_agents']} room={live.get('ws_room')}")
    print(f"  inbox file injected: {live.get('inbox_injected')}")
    print(f"  inbox archived: {live.get('inbox_archived')}")
    print(f"  inbox log line: {live.get('inbox_log')}")
    if live.get("inbox_active_agents"):
        print(f"    active_agents: {live['inbox_active_agents']}")
        print(f"    content: {live.get('inbox_content_prefix')!r}")

    passed = (
        unit_ok
        and live.get("slash_not_injected")
        and live.get("ws_injected")
        and live.get("inbox_injected")
        and live.get("inbox_archived")
    )
    print(f"\n{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))