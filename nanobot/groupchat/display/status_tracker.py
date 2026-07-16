"""Live agent status dashboard (view layer — UI states, not runtime busy/idle)."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from loguru import logger

from nanobot.groupchat.display import display as _d

# ── Tool-name → status state mapping ─────────────────────────
_TOOL_STATE_MAP: dict[str, str] = {
    "web_search": "searching",
    "web_fetch": "fetching",
    "exec": "executing",
    "read_file": "reading",
    "write_file": "writing",
    "edit_file": "writing",
    "list_dir": "reading",
    "chatroom_send": "sending",
    "wait": "waiting",
    "interrupted": "interrupted",
}


class AgentStatusTracker:
    """UI status panel (thinking/searching/…), not AgentRunner busy/idle.

    One Telegram message edited in-place. Concurrent-safe on one event loop.
    No-op when edit_fn is unavailable (e.g. CLI).
    """

    EDIT_INTERVAL = 0.8  # seconds — matches StreamingDisplay throttle

    def __init__(
        self,
        agents: list[str],
        leader: str | None,
        edit_fn: Callable[[int, str], Awaitable[None]] | None,
        send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None,
    ):
        self._agents = list(agents)
        self._leader = leader
        self._edit_fn = edit_fn
        self._send_and_get_id = send_and_get_id_fn
        self._msg_id: int | None = None
        self._states: dict[str, str] = {a: "thinking" for a in agents}
        self._details: dict[str, str] = {a: "" for a in agents}
        self._reasons: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._last_edit: float = 0.0
        self._dirty = False

    async def create_panel(self) -> None:
        """Send the initial status panel message and store its ID."""
        if not self._send_and_get_id:
            return
        try:
            text = self._render()
            self._msg_id = await self._send_and_get_id(text)
        except Exception as e:
            logger.warning("StatusTracker: create_panel failed: {}", e)

    async def set_state(
        self,
        agent: str,
        state: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        """Update an agent's state and trigger a throttled panel refresh."""
        async with self._lock:
            if agent not in self._states:
                return
            self._states[agent] = state
            self._details[agent] = detail
            if reason:
                self._reasons[agent] = reason
            elif state not in ("blocked", "error", "done", "cancelled"):
                self._reasons.pop(agent, None)
            self._dirty = True
        await self._maybe_refresh()

    async def _maybe_refresh(self) -> None:
        """Edit the panel message if dirty and throttle interval has passed."""
        if not self._msg_id or not self._edit_fn or not self._dirty:
            return
        import time
        now = time.time()
        if now - self._last_edit < self.EDIT_INTERVAL:
            return
        async with self._lock:
            if not self._dirty:
                return
            text = self._render()
            self._dirty = False
        self._last_edit = now
        try:
            await self._edit_fn(self._msg_id, text)
        except Exception as e:
            logger.debug("StatusTracker: edit failed: {}", e)

    def _render(self) -> str:
        """Build the panel text using the display module."""
        return _d.status_panel(
            self._agents, self._states, self._details,
            self._reasons, leader=self._leader,
        )

    async def finalize(self) -> None:
        """Force a final panel edit regardless of throttle."""
        if not self._msg_id or not self._edit_fn:
            return
        async with self._lock:
            text = self._render()
            self._dirty = False
        try:
            await self._edit_fn(self._msg_id, text)
        except Exception:
            pass

    def add_agent(self, name: str) -> None:
        """Register a new agent that joined mid-round."""
        if name not in self._states:
            self._agents.append(name)
            self._states[name] = "thinking"
            self._details[name] = ""
            self._dirty = True
    async def update_from_tool_start(self, agent: str, tool_name: str, args: dict) -> None:
        """Update tracker state based on tool arguments."""
        _st = _TOOL_STATE_MAP.get(tool_name, "thinking")
        _dt = ""
        if tool_name == "web_search":
            _dt = (args.get("query") or args.get("queries", ""))
            if isinstance(_dt, list):
                _dt = ", ".join(_dt)
        elif tool_name == "web_fetch":
            _dt = (args.get("url", "") or "")[:35]
        elif tool_name == "exec":
            _dt = (args.get("command", "") or "")[:25]
        elif tool_name in ("read_file", "write_file", "edit_file"):
            _dt = (args.get("path", "") or "").split("/")[-1]
        elif tool_name == "chatroom_send":
            _to = args.get("to", "?")
            _dt = ", ".join(_to) if isinstance(_to, list) else str(_to)
        await self.set_state(agent, _st, detail=str(_dt)[:30])

    async def update_from_tool_result(self, agent: str, tool_name: str, result: str) -> None:
        """Update tracker state based on tool results."""
        _r = result or ""
        if tool_name == "chatroom_send" and "BLOCKED:" in _r:
            await self.set_state(agent, "blocked", reason="pool full")
        elif tool_name == "chatroom_send" and "你已发过 1 条消息" in _r:
            await self.set_state(agent, "blocked", reason="leader gate")
        elif tool_name == "web_search" and "BLOCKED:" in _r and "额度" in _r:
            await self.set_state(agent, "blocked", reason="no credits")
        elif tool_name == "web_search" and "BLOCKED:" in _r and "本轮已搜索" in _r:
            await self.set_state(agent, "blocked", reason="cycle limit")
        else:
            await self.set_state(agent, "thinking")
