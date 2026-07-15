"""Working memory vs shared History.

Two layers of conversation state in groupchat:

1. **History** (``engine.history`` / ``nanobot.core.history.History``)
   Shared durable transcript. Teammates, persistence, compress, and the next
   broadcast round all read this. Cycle outputs MUST commit here.

2. **Working memory** (this module — per-agent ``list[dict]``)
   Ephemeral LLM session for the current tool_loop multi-turn protocol
   (assistant/tool messages, interrupt injects, system nudges). Private to one
   agent task; never read by teammates.

Rules
-----
- Shared context comes from History via ``engine._build_agent_prompt`` /
  ``History.build_for_groupchat`` — never from another agent's working memory.
- After a cycle produces text/tools, call ``commit_agent_turn`` so History is
  the truth source.
- After wait/interrupt reactivation, prefer ``WorkingMemory.refresh`` (rebuild
  from History) over unbounded append+prune of the local list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from nanobot.groupchat.orchestra.chat_utils import build_tool_log


def commit_agent_turn(
    engine: Any,
    agent: str,
    content: str | None,
    tool_calls_detail: list[dict[str, Any]] | None = None,
) -> str:
    """Commit one agent cycle output into shared History.

    Builds the same ``content + tool_log`` payload the broadcast/direct paths
    historically wrote via ``engine._add_message``. Returns the committed string
    (empty if nothing to write).
    """
    history_content = (content or "") + build_tool_log(tool_calls_detail or [])
    if not history_content:
        return ""
    engine._add_message(agent, history_content)
    return history_content


@dataclass
class WorkingMemory:
    """Ephemeral per-agent LLM message list.

    Parameters
    ----------
    messages:
        Live list passed to ``tool_loop``.
    role_injections:
        System (or other) messages inserted *before the volatile tail* at
        startup (leader hint, team rules, perm placeholder). Re-applied in
        the same insert order on ``refresh``.
    """

    messages: list[dict[str, Any]]
    role_injections: list[dict[str, Any]] = field(default_factory=list)

    # ── mutation helpers ────────────────────────────────────────────────

    def insert_before_last(self, msg: dict[str, Any], *, track: bool = True) -> None:
        """Insert before the last message (volatile status / last user turn).

        Matches the historical ``messages.insert(max(len-1, 0), …)`` pattern
        so successive inserts stack immediately before the tail (first insert is left-most among them).
        """
        idx = max(len(self.messages) - 1, 0)
        self.messages.insert(idx, msg)
        if track:
            self.role_injections.append(msg)

    def append(self, msg: dict[str, Any]) -> None:
        self.messages.append(msg)

    def extend(self, msgs: list[dict[str, Any]]) -> None:
        self.messages.extend(msgs)

    @property
    def sys_msg_count(self) -> int:
        """Length used as stable-prefix boundary for optional prune helpers."""
        return len(self.messages)

    def refresh(
        self,
        build_prompt: Callable[[], list[dict[str, Any]]],
        trailing: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Rebuild working memory from History-backed prompt + role injections.

        Call after wait/interrupt once the prior cycle is already committed to
        History. Replaces ``prune_conversation_tail`` + manual re-inject of own
        prior text — History already holds committed turns.
        """
        base = build_prompt()
        for inj in self.role_injections:
            base.insert(max(len(base) - 1, 0), inj)
        if trailing:
            base.extend(trailing)
        self.messages = base
        return self.messages


__all__ = ["WorkingMemory", "commit_agent_turn"]