"""Working memory vs shared History.

Two layers of conversation state in groupchat:

1. **History** (``engine.history`` / ``nanobot.core.history.History``)
   Shared durable transcript. Teammates, persistence, compress, and the next
   broadcast round all read this. Cycle outputs MUST commit here.

2. **Working memory** (this module — per-agent ``list[dict]``)
   Ephemeral LLM session for the current tool_loop multi-turn protocol
   (assistant/tool messages within one cycle). Private to one agent task.

Rules
-----
- Shared context comes from History via ``engine._build_agent_prompt`` /
  ``History.build_for_groupchat`` — never from another agent's working memory.
- After a cycle produces text/tools, call ``commit_agent_turn``.
- Any re-entry into tool_loop (wait / interrupt / system nudge) must
  ``refresh`` from History rather than append onto a drifted private list.
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
        System messages inserted *before the volatile tail* at startup
        (leader hint, team rules, perm placeholder). Re-applied on ``refresh``.
    trailing_count:
        Number of ephemeral trailing messages after the volatile user turn
        (interrupt/wait/nudge injects). Used to locate the volatile message
        for status-summary updates without a stale absolute index.
    """

    messages: list[dict[str, Any]]
    role_injections: list[dict[str, Any]] = field(default_factory=list)
    trailing_count: int = 0

    # ── mutation helpers ────────────────────────────────────────────────

    def insert_before_last(self, msg: dict[str, Any], *, track: bool = True) -> None:
        """Insert before the last message (volatile status / last user turn).

        Matches the historical ``messages.insert(max(len-1, 0), …)`` pattern
        so successive inserts stack immediately before the tail (first insert
        is left-most among them).
        """
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
        """Index of PromptBuilder's volatile user message (status summary target).

        Layout after refresh::

            [ system/static … | role_injections… | volatile_user | trailing… ]
                                                         ^
                                                   volatile_index
        """
        return max(0, len(self.messages) - 1 - self.trailing_count)

    def refresh(
        self,
        build_prompt: Callable[[], list[dict[str, Any]]],
        trailing: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Rebuild working memory from History-backed prompt + role injections.

        Call on every tool_loop re-entry once prior cycle output (if any) is
        committed to History. ``trailing`` holds only ephemeral injects for
        this re-entry (wait msg / interrupt / system nudge).
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
        """Convenience: refresh with zero or more trailing inject messages."""
        trailing = list(trailing_msgs) if trailing_msgs else None
        return self.refresh(build_prompt, trailing=trailing)


__all__ = ["WorkingMemory", "commit_agent_turn"]
