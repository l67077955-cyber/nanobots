"""Live agent status dashboard (view layer — UI states, not runtime busy/idle)."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from loguru import logger

from nanobot.groupchat.display import display as _d

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

_TERMINAL = frozenset({"done", "error", "cancelled"})


class AgentStatusTracker:
    """UI status panel (thinking/searching/…), not AgentRunner busy/idle.

    One Telegram message edited in-place. Heartbeat advances elapsed seconds
    during long LLM/wait gaps (no config knobs — fixed intervals).
    """

    EDIT_INTERVAL = 0.8
    HEARTBEAT_INTERVAL = 1.2

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
        now = time.monotonic()
        self._states: dict[str, str] = {a: "thinking" for a in agents}
        self._details: dict[str, str] = {a: "" for a in agents}
        self._reasons: dict[str, str] = {}
        self._state_since: dict[str, float] = {a: now for a in agents}
        self._lock = asyncio.Lock()
        self._last_edit: float = 0.0
        self._dirty = False
        self._heartbeat_task: asyncio.Task | None = None
        self._pending_refresh_handle: asyncio.TimerHandle | None = None

    async def create_panel(self) -> None:
        if not self._send_and_get_id:
            return
        try:
            text = self._render()
            self._msg_id = await self._send_and_get_id(text)
            self._last_edit = time.time()
        except Exception as e:
            logger.warning("StatusTracker: create_panel failed: {}", e)
            return
        self.start_heartbeat()

    def start_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        if not self._msg_id or not self._edit_fn:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._heartbeat_task = loop.create_task(self._heartbeat_loop())

    def stop_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None
        if self._pending_refresh_handle is not None:
            self._pending_refresh_handle.cancel()
            self._pending_refresh_handle = None

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if not self._msg_id or not self._edit_fn:
                    continue
                if all(self._states.get(a) in _TERMINAL for a in self._agents):
                    continue
                self._dirty = True
                await self._maybe_refresh()
        except asyncio.CancelledError:
            return

    async def set_state(
        self,
        agent: str,
        state: str,
        detail: str = "",
        reason: str = "",
    ) -> None:
        async with self._lock:
            if agent not in self._states:
                return
            prev_state = self._states.get(agent)
            prev_detail = self._details.get(agent, "")
            self._states[agent] = state
            self._details[agent] = detail
            if reason:
                self._reasons[agent] = reason
            elif state not in ("blocked", "error", "done", "cancelled"):
                self._reasons.pop(agent, None)
            if prev_state != state or prev_detail != detail:
                self._state_since[agent] = time.monotonic()
            self._dirty = True
        await self._maybe_refresh()

    async def _maybe_refresh(self, *, force: bool = False) -> None:
        if not self._msg_id or not self._edit_fn:
            return
        if not self._dirty and not force:
            return
        now = time.time()
        remaining = self.EDIT_INTERVAL - (now - self._last_edit)
        if not force and remaining > 0:
            if self._pending_refresh_handle is None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return

                def _fire() -> None:
                    self._pending_refresh_handle = None
                    try:
                        loop.create_task(self._maybe_refresh(force=True))
                    except Exception:
                        pass

                self._pending_refresh_handle = loop.call_later(remaining, _fire)
            return

        async with self._lock:
            if not self._dirty and not force:
                return
            text = self._render()
            self._dirty = False
        self._last_edit = time.time()
        try:
            await self._edit_fn(self._msg_id, text)
        except Exception as e:
            logger.debug("StatusTracker: edit failed: {}", e)

    def _render(self) -> str:
        now = time.monotonic()
        elapsed = {
            a: max(0, int(now - self._state_since.get(a, now)))
            for a in self._agents
        }
        return _d.status_panel(
            self._agents,
            self._states,
            self._details,
            self._reasons,
            leader=self._leader,
            elapsed_s=elapsed,
        )

    async def finalize(self) -> None:
        self.stop_heartbeat()
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
        if name not in self._states:
            self._agents.append(name)
            self._states[name] = "thinking"
            self._details[name] = ""
            self._state_since[name] = time.monotonic()
            self._dirty = True

    async def update_from_tool_start(self, agent: str, tool_name: str, args: dict) -> None:
        _st = _TOOL_STATE_MAP.get(tool_name, "thinking")
        _dt = ""
        if tool_name == "web_search":
            _dt = args.get("query") or args.get("queries", "")
            if isinstance(_dt, list):
                _dt = ", ".join(str(x) for x in _dt)
        elif tool_name == "web_fetch":
            _dt = (args.get("url", "") or "")[:35]
        elif tool_name == "exec":
            _dt = (args.get("command", "") or "")[:25]
        elif tool_name in ("read_file", "write_file", "edit_file"):
            _dt = (args.get("path", "") or "").split("/")[-1]
        elif tool_name == "chatroom_send":
            _to = args.get("to", "?")
            _dt = ", ".join(_to) if isinstance(_to, list) else str(_to)
        elif tool_name == "wait":
            _from = args.get("from_agent") or args.get("from") or ""
            _dt = str(_from)[:30] if _from else "teammate"
        await self.set_state(agent, _st, detail=str(_dt)[:30])

    async def update_from_tool_result(self, agent: str, tool_name: str, result: str) -> None:
        _r = result or ""
        if tool_name == "chatroom_send" and "BLOCKED:" in _r:
            await self.set_state(agent, "blocked", reason="pool full")
        elif tool_name == "chatroom_send" and "你已发过 1 条消息" in _r:
            await self.set_state(agent, "blocked", reason="leader gate")
        elif tool_name == "web_search" and "BLOCKED:" in _r and "额度" in _r:
            await self.set_state(agent, "blocked", reason="no credits")
        elif tool_name == "web_search" and "BLOCKED:" in _r and "本轮已搜索" in _r:
            await self.set_state(agent, "blocked", reason="cycle limit")
        elif tool_name == "wait" and _r.startswith("⏰"):
            await self.set_state(agent, "thinking", detail="wait timeout")
        else:
            await self.set_state(agent, "thinking")
