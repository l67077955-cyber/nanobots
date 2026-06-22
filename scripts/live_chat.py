#!/usr/bin/env python3
"""Real-time chat with agents via headless web channel."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

WS_URL = "ws://127.0.0.1:18791/?token=nanobot-watch-2026&chat_id=live-chat"
USER_MSG = (
    "大家好，我是用户。请 Harper 和 Kirk 各用一句话自我介绍，"
    "并确认你们现在能收到这条消息。不要调用工具，直接回复。"
)


async def chat(duration_s: float = 180.0) -> None:
    agent_msgs: list[dict] = []
    async with websockets.connect(WS_URL, open_timeout=8) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        print(f"[connected] agents={hello.get('active_agents')}\n")

        for cmd in ("/stop", USER_MSG):
            print(f"[user] {cmd[:80]}{'...' if len(cmd) > 80 else ''}")
            await ws.send(json.dumps({"type": "message", "content": cmd}))
            if cmd == "/stop":
                await asyncio.sleep(1.5)
                continue

        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            except asyncio.TimeoutError:
                if agent_msgs:
                    break
                print("[waiting...]")
                continue
            msg = json.loads(raw)
            if msg.get("type") != "message":
                continue
            role = msg.get("role", "")
            agent = msg.get("agent", "")
            content = (msg.get("content") or "").strip()
            progress = msg.get("progress")
            if not content or role == "user":
                continue
            if progress and len(content) < 20:
                print(f"[{agent or 'agent'} · progress] {content[:100]}")
                continue
            # skip echo of our own message in code blocks only
            if content in (USER_MSG, "/stop"):
                continue
            entry = {"agent": agent, "content": content, "ts": time.time()}
            agent_msgs.append(entry)
            label = agent or "agent"
            preview = content.replace("\n", " ")[:300]
            print(f"\n[{label}]\n{preview}{'...' if len(content) > 300 else ''}\n")

            # stop after both agents said something substantive
            names = {m["agent"] for m in agent_msgs if m["agent"] and len(m["content"]) > 30}
            if {"Harper", "Kirk"}.issubset(names):
                print("[both agents replied, done]")
                break

    print(f"\n=== summary: {len(agent_msgs)} agent messages ===")
    for m in agent_msgs:
        print(f"- {m['agent']}: {m['content'][:120].replace(chr(10), ' ')}")


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    asyncio.run(chat(dur))