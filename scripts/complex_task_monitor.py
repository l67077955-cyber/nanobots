#!/usr/bin/env python3
"""Send a complex delivery task and monitor agent progress."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import websockets

WS_URL = "ws://127.0.0.1:18791/?token=nanobot-watch-2026&chat_id=complex-delivery"
GATEWAY_LOG = Path("/root/.nanobot/logs/gateway.log")
ROOM_EVENTS = Path("/root/.nanobot/logs/room_events.jsonl")
REPO = Path("/root/nanobot-src")

TASK = """\
【复杂交付任务 — 必须产出代码+测试+评价，禁止空聊】

背景：今早 gc-20260620-141846 修了 Telegram callback 按钮和 broadcast 冻结 prompt 问题，
但 agent 只 edit_file 没有验证、没有重启、没有交付。

请 Harper（建构/交付）和 Kirk（审查/质疑）协作完成以下可验收交付：

## 目标
审计并补强「broadcast 期间用户插话 → live prefix 重建」+「分级消息裁剪」两条链路。

## 必须交付（缺一不可）
1. **代码审计报告**（markdown）：逐文件列出
   - `nanobot/groupchat/orchestra/broadcast_agent.py` 中 `_apply_live_prefix` 触发条件
   - `nanobot/groupchat/history/message_converter.py` 中 tier 优先级逻辑
   - 仍存在的缺口（例如 wait 非 rebuild 分支仍用 `prune_conversation_tail_with_summary`）
   每条结论附 `文件:行号` 证据。

2. **集成测试** `tests/test_broadcast_live_prefix.py`：
   - wait() 唤醒时用户插话 → prefix 重建（mock engine._history 增长）
   - interrupt 路径用户插话 → prefix 重建
   - 至少 4 个测试，全部 pass

3. **若发现缺口则修复代码**（优先修非 rebuild 分支），并 `pytest` 验证。

4. **交付物路径**写在最终 synthesis 里：
   - 报告文件路径
   - 测试文件路径
   - pytest 输出摘要（passed/failed 数量）

## 约束
- 必须实际 read_file / exec(pytest)，禁止只靠记忆编造行号
- Kirk 必须对 Harper 的每个结论做证据挑战（要求 grep/行号）
- 完成后 Harper synthesis 给用户，不要中途停在「我去看看」
- 工作目录：/root/nanobot-src
"""


def _log_stats(since_pos: int) -> dict:
    text = GATEWAY_LOG.read_text(encoding="utf-8", errors="replace")[since_pos:]
    return {
        "rebuild_prefix": len(re.findall(r"rebuilt prompt prefix", text)),
        "tool_exec": len(re.findall(r"tool.*exec|exec\(", text, re.I)),
        "read_file": len(re.findall(r"read_file", text, re.I)),
        "write_file": len(re.findall(r"write_file", text, re.I)),
        "edit_file": len(re.findall(r"edit_file", text, re.I)),
        "pytest": len(re.findall(r"pytest", text, re.I)),
        "done": "══ Done" in text or "Leader 结束讨论" in text,
        "errors": len(re.findall(r"ERROR|Traceback|failed", text)),
    }


def _check_deliverables() -> dict:
    report_candidates = list(REPO.glob("**/*audit*")) + list(REPO.glob("**/*broadcast*report*"))
    report_candidates += list(Path("/root/.nanobot/workspace").glob("**/*.md"))
    test_file = REPO / "tests" / "test_broadcast_live_prefix.py"
    return {
        "test_file_exists": test_file.exists(),
        "test_file_lines": len(test_file.read_text().splitlines()) if test_file.exists() else 0,
        "md_reports": [str(p.relative_to(REPO)) for p in report_candidates if p.suffix == ".md" and p.stat().st_mtime > time.time() - 3600][:5],
    }


async def run(duration_s: float = 900.0) -> int:
    since_pos = GATEWAY_LOG.stat().st_size if GATEWAY_LOG.exists() else 0
    outputs: list[str] = []

    async with websockets.connect(WS_URL, open_timeout=10) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        print(f"[connected] agents={hello.get('active_agents')}\n")
        print("[task sent]\n" + TASK[:400] + "...\n")

        await ws.send(json.dumps({"type": "message", "content": "/stop"}))
        await asyncio.sleep(1.5)
        await ws.send(json.dumps({"type": "message", "content": TASK}))

        deadline = time.monotonic() + duration_s
        last_stat = 0.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20.0)
            except asyncio.TimeoutError:
                if time.monotonic() - last_stat > 30:
                    stats = _log_stats(since_pos)
                    dels = _check_deliverables()
                    print(f"[poll] rebuild={stats['rebuild_prefix']} read={stats['read_file']} "
                          f"write={stats['write_file']} pytest={stats['pytest']} "
                          f"test_file={dels['test_file_exists']} done={stats['done']}")
                    last_stat = time.monotonic()
                    if stats["done"] and dels["test_file_exists"]:
                        await asyncio.sleep(15)
                        break
                continue

            msg = json.loads(raw)
            c = (msg.get("content") or "").strip()
            if msg.get("type") != "message" or not c or msg.get("role") == "user":
                continue
            if msg.get("progress") and len(c) < 30:
                continue
            # capture substantive outputs
            if any(k in c for k in ("Output", "Done", "synthesis", "交付", "pytest", "test_broadcast")):
                if c not in outputs:
                    outputs.append(c)
                    preview = c.replace("\n", " ")[:280]
                    print(f"\n>>> {preview}...\n")

    stats = _log_stats(since_pos)
    dels = _check_deliverables()
    print("\n" + "=" * 60)
    print("FINAL STATS")
    print(json.dumps(stats, indent=2))
    print("DELIVERABLES")
    print(json.dumps(dels, indent=2))
    print(f"OUTPUT SNIPPETS: {len(outputs)}")

    passed = dels["test_file_exists"] and stats["read_file"] >= 3 and stats["done"]
    print(f"\n{'DELIVERY OK' if passed else 'DELIVERY INCOMPLETE'}")
    return 0 if passed else 1


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 900.0
    raise SystemExit(asyncio.run(run(dur)))