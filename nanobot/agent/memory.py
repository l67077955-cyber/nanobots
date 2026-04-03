"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None

_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(m in text for m in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """Multi-file RAG memory system (inspired by Claude Code memdir).

    Storage structure:
        memory/
        ├── MEMORY.md          ← 索引文件（每行 ≤150字，≤200行）
        ├── HISTORY.md         ← 追加式时间线日志
        ├── user_profile.md    ← 独立记忆文件（带 frontmatter）
        ├── feedback_style.md
        ├── project_bugs.md
        └── reference_tools.md

    Memory types (from Claude Code):
        user      — 用户画像（角色、目标、偏好）
        feedback  — 行为反馈（纠正、确认）
        project   — 项目上下文（进度、决策、Bug）
        reference — 外部引用（链接、工具位置）

    Each memory file uses YAML frontmatter:
        ---
        name: 用户偏好
        description: 用户不喜欢每次回复末尾的总结
        type: feedback
        ---
        具体内容...
    """

    MEMORY_TYPES = ("user", "feedback", "project", "reference")
    MAX_INDEX_LINES = 200
    MAX_INDEX_BYTES = 25_000
    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._consecutive_failures = 0

    # ── Legacy interface (backward-compatible) ────────────────

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # ── RAG: Multi-file memory operations ─────────────────────

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
        """Parse YAML frontmatter from a markdown file.

        Returns (frontmatter_dict, body_text).
        """
        if not content.startswith("---"):
            return {}, content

        lines = content.split("\n")
        end_idx = -1
        for i in range(1, min(len(lines), 30)):  # Cap search at 30 lines
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx < 0:
            return {}, content

        fm: dict[str, str] = {}
        for line in lines[1:end_idx]:
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip().strip("'\"")

        body = "\n".join(lines[end_idx + 1:]).strip()
        return fm, body

    def scan_memories(self) -> list[dict[str, Any]]:
        """Scan memory/ for .md files and parse frontmatter headers.

        Returns list of dicts sorted by mtime (newest first):
            [{"filename": "user_role.md", "path": "/abs/path",
              "name": "...", "description": "...", "type": "user",
              "mtime": 1712345678.0, "size": 1234}, ...]

        Excludes MEMORY.md and HISTORY.md (those are index/log, not memories).
        """
        results: list[dict[str, Any]] = []
        skip = {"MEMORY.md", "HISTORY.md"}
        if not self.memory_dir.is_dir():
            return results

        for p in self.memory_dir.rglob("*.md"):
            if p.name in skip:
                continue
            try:
                stat = p.stat()
                # Read only first 30 lines for frontmatter (fast)
                head = ""
                with open(p, encoding="utf-8") as f:
                    head_lines = []
                    for i, line in enumerate(f):
                        if i >= 30:
                            break
                        head_lines.append(line)
                    head = "".join(head_lines)

                fm, _ = self._parse_frontmatter(head)
                rel = p.relative_to(self.memory_dir)
                results.append({
                    "filename": str(rel),
                    "path": str(p),
                    "name": fm.get("name", p.stem.replace("_", " ")),
                    "description": fm.get("description", ""),
                    "type": fm.get("type", ""),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["mtime"], reverse=True)
        return results[:200]  # Cap like Claude Code

    def read_memory(self, filename: str) -> tuple[dict[str, str], str] | None:
        """Read a specific memory file. Returns (frontmatter, body) or None."""
        path = self.memory_dir / filename
        if not path.exists() or not path.suffix == ".md":
            return None
        try:
            content = path.read_text(encoding="utf-8")
            return self._parse_frontmatter(content)
        except Exception:
            return None

    def write_memory(
        self,
        filename: str,
        content: str,
        *,
        name: str = "",
        description: str = "",
        memory_type: str = "",
    ) -> Path:
        """Write a memory file with YAML frontmatter.

        Args:
            filename: e.g. "user_role.md" or "subdir/topic.md"
            content: Body text (without frontmatter)
            name: Human-readable title
            description: One-line hook for index/search
            memory_type: One of MEMORY_TYPES (user/feedback/project/reference)
        """
        path = self.memory_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)

        if not name:
            name = path.stem.replace("_", " ").title()
        if memory_type and memory_type not in self.MEMORY_TYPES:
            memory_type = ""

        fm_lines = ["---"]
        fm_lines.append(f"name: {name}")
        if description:
            fm_lines.append(f"description: {description}")
        if memory_type:
            fm_lines.append(f"type: {memory_type}")
        fm_lines.append("---")
        fm_lines.append("")

        full = "\n".join(fm_lines) + content.strip() + "\n"
        path.write_text(full, encoding="utf-8")
        logger.info("Memory written: {} ({} bytes)", filename, len(full))
        return path

    def delete_memory(self, filename: str) -> bool:
        """Delete a memory file."""
        path = self.memory_dir / filename
        if path.exists() and path.suffix == ".md" and path.name not in ("MEMORY.md", "HISTORY.md"):
            path.unlink()
            logger.info("Memory deleted: {}", filename)
            return True
        return False

    def build_memory_index(self) -> str:
        """Generate an index of all memory files (like Claude Code's MEMORY.md).

        Format: one line per entry, ≤150 chars:
            - [Title](filename.md) — description
        """
        memories = self.scan_memories()
        if not memories:
            return ""

        lines: list[str] = []
        for m in memories:
            name = m["name"]
            fn = m["filename"]
            desc = m["description"]
            tag = f"[{m['type']}] " if m["type"] else ""
            if desc:
                line = f"- {tag}[{name}]({fn}) — {desc}"
            else:
                line = f"- {tag}[{name}]({fn})"
            # Truncate long lines
            if len(line) > 150:
                line = line[:147] + "..."
            lines.append(line)

        return "\n".join(lines[:self.MAX_INDEX_LINES])

    def search_memories(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search all memory files for a query string (case-insensitive grep).

        Returns list of matches:
            [{"filename": "...", "line_num": 5, "line": "matching line..."}]
        """
        query_lower = query.lower()
        results: list[dict[str, Any]] = []
        skip = {"MEMORY.md", "HISTORY.md"}

        if not self.memory_dir.is_dir():
            return results

        for p in self.memory_dir.rglob("*.md"):
            if p.name in skip:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if query_lower in line.lower():
                            rel = p.relative_to(self.memory_dir)
                            results.append({
                                "filename": str(rel),
                                "line_num": i,
                                "line": line.strip()[:200],
                            })
                            if len(results) >= max_results:
                                return results
            except Exception:
                continue
        return results

    def format_memory_manifest(self) -> str:
        """Format memory headers as a text manifest for LLM selection.

        Output format (like Claude Code):
            - [type] filename (ISO timestamp): description
        """
        memories = self.scan_memories()
        if not memories:
            return "(无记忆文件)"

        lines: list[str] = []
        for m in memories:
            tag = f"[{m['type']}] " if m["type"] else ""
            ts = datetime.fromtimestamp(m["mtime"]).strftime("%Y-%m-%d %H:%M")
            if m["description"]:
                lines.append(f"- {tag}{m['filename']} ({ts}): {m['description']}")
            else:
                lines.append(f"- {tag}{m['filename']} ({ts})")
        return "\n".join(lines)

    # ── Legacy formatting ─────────────────────────────────────

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into MEMORY.md + HISTORY.md."""
        if not messages:
            return True

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        chat_messages = [
            {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(
                response.content
            ):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            if "history_entry" not in args or "memory_update" not in args:
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return self._fail_or_raw_archive(messages)

            entry = args["history_entry"]
            update = args["memory_update"]

            if entry is None or update is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return self._fail_or_raw_archive(messages)

            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages)

            self.append_history(entry)
            update = _ensure_text(update)
            if update != current_memory:
                self.write_long_term(update)

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to HISTORY.md without LLM summarization."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive a selected message chunk into persistent memory."""
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens.
        
        Scans from the beginning of messages (not from an offset) to find
        the largest chunk that can be replaced with a summary.
        """
        if not session.messages or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(len(session.messages)):
            message = session.messages[idx]
            if idx > 0 and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=None)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive messages with guaranteed persistence (retries until raw-dump fallback)."""
        if not messages:
            return True
        for _ in range(self.store._MAX_FAILURES_BEFORE_RAW_ARCHIVE):
            if await self.consolidate_messages(messages):
                return True
        return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: replace old messages with summaries until prompt fits within half the context window.
        
        Unlike the old offset-based approach, this replaces messages in-place with a single
        summary message, preserving the prefix of the messages list for cache stability.
        """
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            target = self.context_window_tokens // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < self.context_window_tokens:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )

                # Build a brief summary text for the in-place replacement message
                summary_text = self._build_chunk_summary(chunk)

                if not await self.consolidate_messages(chunk):
                    return

                # Replace the chunk with a single summary message (in-place, preserves prefix)
                summary_msg = {
                    "role": "system",
                    "content": f"[Consolidated {len(chunk)} messages into memory. Summary: {summary_text}]",
                    "consolidated": True,
                    "timestamp": datetime.now().isoformat(),
                }
                session.messages = [summary_msg] + session.messages[end_idx:]
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return

    @staticmethod
    def _build_chunk_summary(messages: list[dict]) -> str:
        """Build a brief text summary of a message chunk for in-place replacement."""
        # Collect user messages and tool names for a compact summary
        user_msgs = []
        tools_used = set()
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content and len(content) > 10:
                # Truncate long messages
                user_msgs.append(content[:120] + ("..." if len(content) > 120 else ""))
            if msg.get("tools_used"):
                tools_used.update(msg["tools_used"])
            # Check tool_calls
            for tc in (msg.get("tool_calls") or []):
                if isinstance(tc, dict) and tc.get("function", {}).get("name"):
                    tools_used.add(tc["function"]["name"])

        parts = []
        if user_msgs:
            parts.append(f"Topics: {'; '.join(user_msgs[:5])}")
        if tools_used:
            parts.append(f"Tools: {', '.join(sorted(tools_used)[:8])}")
        return " | ".join(parts) if parts else f"{len(messages)} messages processed"
