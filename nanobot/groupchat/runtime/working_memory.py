"""Ephemeral tool-protocol buffer — NOT a second context store.

Architecture
------------
**History** (``nanobot.core.history.History``) is the *only* layer that
owns / processes durable conversation context.

This module holds a **short-lived** ``list[dict]`` for one agent's
in-flight ``tool_loop`` multi-step protocol (assistant / tool messages
within a single cycle). It is collaboration plumbing, not context logic.

Rules
-----
1. Shared durable context is always ``engine.history`` (via commit).
2. After a cycle produces text/tools → ``commit_agent_turn`` → History.
3. Any re-entry (wait / interrupt / system nudge) → ``refresh`` from a
   History-backed prompt builder — never grow a private list that can
   desync from History.
4. display must never touch WorkingMemory or History writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from nanobot.groupchat.runtime.chat_utils import build_tool_log


def commit_agent_turn(
    engine: Any,
    agent: str,
    content: str | None,
    tool_calls_detail: list[dict[str, Any]] | None = None,
) -> str:
    """Format cycle output and commit into History (sole context write).

    Tool-log text is pure formatting for storage; the durable write is
    ``History.commit_turn``. Engine persistence is an I/O hook only.
    """
    history_content = (content or "") + build_tool_log(tool_calls_detail or [])
    if not history_content:
        return ""

    history = getattr(engine, "history", None)
    if history is None:
        raise RuntimeError("commit_agent_turn requires engine.history (History)")

    # Context logic: History only
    committed = history.commit_turn(agent, history_content)

    # I/O hooks (persist / audit) — not context processing
    persist = getattr(engine, "_persist_after_history_write", None)
    if callable(persist):
        persist(agent, committed)
    elif hasattr(engine, "_add_message"):
        # Test doubles may only implement _add_message; avoid double-write
        # if _add_message itself commits to history.
        # Prefer _persist_after_history_write on real engines.
        pass
    return committed


@dataclass
class WorkingMemory:
    """Per-agent ephemeral LLM message list for one tool_loop cycle.

    Not durable. Not shared across agents. Not a History replacement.
    """

    messages: list[dict[str, Any]]
    role_injections: list[dict[str, Any]] = field(default_factory=list)
    trailing_count: int = 0

    def insert_before_last(self, msg: dict[str, Any], *, track: bool = True) -> None:
        """Insert before the last message (volatile status / last user turn)."""
        idx = max(len(self.messages) - 1, 0)
        self.messages.insert(idx, msg)
        if track:
            self.role_injections.append(msg)

    def append(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)
        self.trailing_count += 1

    def extend(self, msgs: list[dict[str, Any]]) -> None:
        self.messages.extend(msgs)
        self.trailing_count += len(msgs)

    @property
    def sys_msg_count(self) -> int:
        """Stable-prefix boundary: everything before ephemeral trailing."""
        return max(0, len(self.messages) - self.trailing_count)

    @property
    def volatile_index(self) -> int:
        """Index of PromptBuilder's volatile user message."""
        return max(0, len(self.messages) - 1 - self.trailing_count)

    def refresh(
        self,
        build_prompt: Callable[[], list[dict[str, Any]]],
        trailing: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Rebuild from History-backed prompt + role injections + trailing.

        ``build_prompt`` must read History (e.g. PromptBuilder); never
        another agent's WorkingMemory.
        """
        base = build_prompt()
        for inj in self.role_injections:
            base.insert(max(len(base) - 1, 0), inj)
        trail = list(trailing or [])
        if trail:
            base.extend(trail)
        self.messages = base
        self.trailing_count = len(trail)
        return self.messages

    def reenter(
        self,
        build_prompt: Callable[[], list[dict[str, Any]]],
        *trailing_msgs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        trailing = list(trailing_msgs) if trailing_msgs else None
        return self.refresh(build_prompt, trailing=trailing)


__all__ = ["WorkingMemory", "commit_agent_turn"]
