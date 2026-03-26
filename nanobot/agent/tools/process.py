"""Process management tool for background exec sessions.

Provides list, poll, kill, and log actions for managing backgrounded
commands started with exec(background=true).

Inspired by OpenClaw's bash-tools.process.ts.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.process_registry import (
    get_any,
    get_session,
    kill_session,
    list_all,
    prune_expired,
    remove_session,
)


class ProcessTool(Tool):
    """Tool to manage background exec sessions."""

    @property
    def name(self) -> str:
        return "process"

    @property
    def description(self) -> str:
        return (
            "Manage background exec sessions. Actions: "
            "list (show all sessions), "
            "poll (get new output from a session, with optional wait), "
            "log (get full aggregated output), "
            "kill (terminate a running session)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "poll", "kill", "log"],
                    "description": "The action to perform",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID (required for poll, kill, log)",
                },
                "wait_ms": {
                    "type": "integer",
                    "description": (
                        "For poll: wait up to N milliseconds for new output "
                        "or process exit before returning (max 120000)"
                    ),
                    "minimum": 0,
                    "maximum": 120_000,
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str = "list",
        session_id: str | None = None,
        wait_ms: int | None = None,
        **kwargs: Any,
    ) -> str:
        # Prune expired finished sessions on every call
        prune_expired()

        if action == "list":
            return self._action_list()
        elif action == "poll":
            if not session_id:
                return "Error: session_id is required for poll"
            return await self._action_poll(session_id, wait_ms)
        elif action == "kill":
            if not session_id:
                return "Error: session_id is required for kill"
            return self._action_kill(session_id)
        elif action == "log":
            if not session_id:
                return "Error: session_id is required for log"
            return self._action_log(session_id)
        else:
            return f"Error: Unknown action '{action}'. Use list, poll, kill, or log."

    def _action_list(self) -> str:
        sessions = list_all()
        if not sessions:
            return "No background sessions."

        lines = []
        for s in sessions:
            status = "exited" if s.exited else "running"
            runtime = time.time() - s.started_at
            exit_info = f" exit={s.exit_code}" if s.exited else ""
            cmd_preview = s.command if len(s.command) <= 80 else s.command[:77] + "..."
            lines.append(
                f"{s.id}  {status:8s}  {runtime:6.1f}s{exit_info}  {cmd_preview}"
            )
        return "\n".join(lines)

    async def _action_poll(self, session_id: str, wait_ms: int | None) -> str:
        session = get_any(session_id)
        if not session:
            return f"No session found for {session_id}"

        # Optional wait for new output or exit
        effective_wait = min(wait_ms or 0, 120_000)
        if effective_wait > 0 and not session.exited:
            deadline = time.time() + effective_wait / 1000
            while not session.exited and time.time() < deadline:
                await asyncio.sleep(min(0.25, max(0, deadline - time.time())))

        stdout, stderr = session.drain()
        output_parts = []
        if stdout:
            output_parts.append(stdout.rstrip())
        if stderr:
            output_parts.append(f"STDERR:\n{stderr.rstrip()}")
        output = "\n".join(output_parts).strip()

        if session.exited:
            exit_info = f"\n\nProcess exited with code {session.exit_code}."
            return (output or "(no new output)") + exit_info
        else:
            return (output or "(no new output)") + "\n\nProcess still running."

    def _action_kill(self, session_id: str) -> str:
        result = kill_session(session_id)
        return result

    def _action_log(self, session_id: str) -> str:
        session = get_any(session_id)
        if not session:
            return f"No session found for {session_id}"

        status = "exited" if session.exited else "running"
        exit_info = f" (exit_code={session.exit_code})" if session.exited else ""
        header = (
            f"Session {session_id} [{status}{exit_info}] "
            f"runtime={session.runtime_s:.1f}s total_output={session.total_output_chars:,}c\n"
            f"{'─' * 60}\n"
        )
        return header + (session.aggregated or "(no output)")
