#!/usr/bin/env python3
"""Headless gateway smoke test — WS connect, slash commands, prompt rebuild."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets

from nanobot.groupchat.orchestra.broadcast_agent import _rebuild_prompt_prefix
from nanobot.groupchat.history.message_converter import latest_user_question


WS_URL = "ws://127.0.0.1:18791/?token=nanobot-watch-2026&chat_id=smoke-test"


async def ws_smoke() -> dict:
    results: dict = {"connected": False, "help_reply": False, "errors": []}
    try:
        async with websockets.connect(WS_URL, open_timeout=5) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(raw)
            results["connected"] = data.get("type") == "connected"
            results["active_agents"] = data.get("active_agents", [])

            await ws.send(json.dumps({"type": "message", "content": "/help"}))
            for _ in range(20):
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                payload = json.loads(msg)
                if payload.get("type") == "message" and payload.get("content"):
                    text = payload["content"]
                    if "命令" in text or "help" in text.lower() or "/" in text:
                        results["help_reply"] = True
                        results["help_snippet"] = text[:120]
                        break
    except Exception as exc:
        results["errors"].append(str(exc))
    return results


def prompt_rebuild_smoke() -> dict:
    """Verify frozen user_question updates when engine.history grows."""
    from nanobot.core.history import History
    engine = MagicMock()
    engine.history = History.from_sender_dicts([
        {"sender": "用户", "content": "原始问题：修按钮"},
    ])
    engine._build_agent_prompt = MagicMock(
        side_effect=lambda _name, **kw: [
            {"role": "system", "content": f"uq={kw.get('user_question', '')}"},
            {"role": "user", "content": kw.get("user_question", "")},
        ]
    )
    engine.get_agent_enabled_tool_names = MagicMock(return_value=["read_file"])

    frozen_uq = "原始问题：修按钮"
    prefix1, live1 = _rebuild_prompt_prefix(
        engine,
        "Kirk",
        agent_ranks={"Kirk": 1},
        agent_idx=0,
        total=1,
        teammates=[],
        user_question=frozen_uq,
        is_leader=True,
        leader_name="Kirk",
        non_leader_agents=[],
    )
    assert live1 == frozen_uq

    engine.history._semantic_add_from_sender("用户", "澄清：是 Telegram callback 按钮无响应")
    prefix2, live2 = _rebuild_prompt_prefix(
        engine,
        "Kirk",
        agent_ranks={"Kirk": 1},
        agent_idx=0,
        total=1,
        teammates=[],
        user_question=frozen_uq,
        is_leader=True,
        leader_name="Kirk",
        non_leader_agents=[],
    )

    rebuilt = live2 != frozen_uq and "澄清" in live2
    prompt_calls = [c.kwargs.get("user_question", "") for c in engine._build_agent_prompt.call_args_list]
    return {
        "latest_user_question": latest_user_question(engine.history.to_sender_dicts()),
        "live_uq_after_clarification": live2,
        "prompt_rebuilt_with_new_uq": rebuilt,
        "build_prompt_user_questions": prompt_calls,
        "prefix_msg_count": len(prefix2),
    }


async def main() -> int:
    print("=== Headless smoke test ===\n")

    print("[1] Prompt rebuild (offline)")
    pr = prompt_rebuild_smoke()
    ok_pr = pr["prompt_rebuilt_with_new_uq"]
    print(f"  latest_user_question: {pr['latest_user_question']!r}")
    print(f"  live_uq after clarification: {pr['live_uq_after_clarification']!r}")
    print(f"  rebuild OK: {ok_pr}")

    print("\n[2] WebSocket /help (live gateway)")
    ws = await ws_smoke()
    print(f"  connected: {ws.get('connected')}")
    print(f"  active_agents: {ws.get('active_agents')}")
    print(f"  help_reply: {ws.get('help_reply')}")
    if ws.get("help_snippet"):
        print(f"  snippet: {ws['help_snippet']!r}")
    if ws.get("errors"):
        print(f"  errors: {ws['errors']}")

    passed = ok_pr and ws.get("connected") and ws.get("help_reply")
    print(f"\n{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))