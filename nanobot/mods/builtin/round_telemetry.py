"""Round telemetry mod — pure observer, writes structured JSONL.

Replaces ad-hoc gateway.log grepping for round/agent/user/tool events.
Every event becomes one JSON line: ``{"ts", "event", ...payload scalars}``.
Output: ``~/.nanobot/mods-telemetry/telemetry.jsonl`` (append, rotated at
10 MB to telemetry.jsonl.1). Disabled by default — opt in via mods.json.

Tier 1 (observe): reads payloads, writes its own file, nothing else.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from nanobot.mods.base import Mod

_MAX_BYTES = 10 * 1024 * 1024


class RoundTelemetryMod(Mod):
    name = "round_telemetry"
    version = "1.0"
    description = "把轮次/agent/用户/工具事件写成结构化 JSONL"

    _path: Path | None = None

    def default_config(self) -> dict[str, Any]:
        return {"out": "~/.nanobot/mods-telemetry/telemetry.jsonl"}

    async def start(self, ctx: Any) -> None:
        out = str(ctx.config.get("out", "~/.nanobot/mods-telemetry/telemetry.jsonl"))
        self._path = Path(out).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        self._path = None

    # One handler per observed event; all funnel into _record.
    async def on_round_started(self, **kw: Any) -> None:
        await self._record("round:started", kw)

    async def on_round_winding_down(self, **kw: Any) -> None:
        await self._record("round:winding_down", kw)

    async def on_round_ended(self, **kw: Any) -> None:
        await self._record("round:ended", kw)

    async def on_user_round_opened(self, **kw: Any) -> None:
        await self._record("user:round_opened", kw)

    async def on_user_message_delivered(self, **kw: Any) -> None:
        await self._record("user:message_delivered", kw)

    async def on_user_message_requeued(self, **kw: Any) -> None:
        await self._record("user:message_requeued", kw)

    async def on_agent_interrupted(self, **kw: Any) -> None:
        await self._record("agent:interrupted", kw)

    async def on_agent_done(self, **kw: Any) -> None:
        await self._record("agent:done", kw)

    async def on_tool_result(self, **kw: Any) -> None:
        await self._record("tool:result", kw)

    async def _record(self, event: str, payload: dict[str, Any]) -> None:
        if self._path is None:
            return
        row = {"ts": round(time.time(), 3), "event": event}
        for k, v in payload.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                row[k] = v
            elif isinstance(v, (list, tuple)) and all(
                isinstance(x, (str, int, float, bool)) for x in v
            ):
                row[k] = list(v)[:20]
        try:
            if self._path.exists() and self._path.stat().st_size > _MAX_BYTES:
                self._path.rename(self._path.with_suffix(".jsonl.1"))
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — telemetry must never break anything
            pass
