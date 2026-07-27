"""Shell execution tool."""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.tools.base import Tool

# Maximum concurrent shell processes in batch mode
_MAX_CONCURRENT_PROCESSES = 8

# Maximum number of commands allowed in a single batch request
_MAX_BATCH_COMMANDS = 16


class ExecTool(Tool):
    """Tool to execute shell commands.

    SECURITY NOTICE:
        The deny_patterns blacklist is a best-effort HINT GUARDRAIL, NOT A SECURITY BOUNDARY.
        It can be bypassed via shell escaping, encoding (base64), variable interpolation,
        pipes (|), xargs, find -delete, etc. Do NOT rely on it for untrusted input.

        For strong isolation, use containerization (Docker, podman), sandboxing (bwrap),
        or a restricted user account. The `restrict_to_workspace` option provides basic
        path confinement but does not protect against all attack vectors.
    """

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = True,  # Changed: default to True for safety
        path_append: str = "",
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
            r"\bxargs\s+rm\b",               # xargs rm bypass
            r"\bfind\s+.*-delete\b",         # find -delete bypass
            r"\bchmod\s+-R\s+000\b",         # chmod -R 000
            r"\b(>|\|\s*)\s*/dev/(null|zero|random|urandom)", # redirect to device
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_BATCH_COMMANDS,
                    "description": f"Multiple shell commands to execute in parallel (batch mode, max {_MAX_BATCH_COMMANDS})",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run command in background. Returns immediately with a session ID. "
                        "Use the process tool (list/poll/kill) to manage the session. "
                        "Use this for long-running commands (>30s)."
                    ),
                },
            },
            "required": [],
        }

    async def execute(
        self, command: str = "", commands: list | None = None,
        working_dir: str | None = None,
        timeout: int | None = None,
        background: bool = False,
        **kwargs: Any,
    ) -> str:
        # Background mode: single command only
        if background and command:
            return await self._run_background(command, working_dir)

        # Batch mode: run all commands concurrently with semaphore
        if commands:
            all_cmds = list(commands)
            if command and command not in all_cmds:
                all_cmds.insert(0, command)
            # Enforce batch size limit
            if len(all_cmds) > _MAX_BATCH_COMMANDS:
                return f"Error: 最多同时执行 {_MAX_BATCH_COMMANDS} 条命令，收到 {len(all_cmds)} 条"
            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROCESSES)

            async def _run_with_semaphore(cmd: str) -> str:
                async with semaphore:
                    return await self._run_one(cmd, working_dir, timeout)

            tasks = [_run_with_semaphore(cmd) for cmd in all_cmds]
            results = await asyncio.gather(*tasks)
            parts = []
            for cmd, result in zip(all_cmds, results):
                parts.append(f"=== $ {cmd} ===\n{result}")
            return "\n\n".join(parts)

        if not command:
            return "Error: 必须提供 command 或 commands 参数"
        return await self._run_one(command, working_dir, timeout)

    async def _run_background(self, command: str, working_dir: str | None = None) -> str:
        """Run a command in background, returning immediately with session info."""
        from nanobot.tools.process_registry import (
            ProcessSession,
            add_session,
            create_session_id,
            start_background_readers,
        )

        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            session_id = create_session_id()
            session = ProcessSession(
                id=session_id,
                command=command,
                pid=process.pid,
                cwd=cwd,
                process=process,
            )
            add_session(session)
            await start_background_readers(session)

            return (
                f"Command running in background (session: {session_id}, pid: {process.pid}).\n"
                f"Use the process tool to manage:\n"
                f"  process(action='poll', session_id='{session_id}') — check output\n"
                f"  process(action='kill', session_id='{session_id}') — terminate\n"
                f"  process(action='list') — list all background sessions"
            )
        except Exception as e:
            return f"Error starting background command: {e}"


    async def _run_one(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        env = os.environ.copy()
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return f"Error: Command timed out after {effective_timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        from nanobot.security.network import contains_internal_url
        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception as e:
                    logger.debug("Could not expand path: {}", e)
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)   # Windows: C:\...
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths
