"""Forget tool — delete previous tool call results from context.

Allows the agent to selectively remove tool call+result pairs from the
conversation history so they don't consume tokens or influence future
LLM calls.

Usage modes:
  - By keyword:  forget(keywords="curl xxx")   — matches tool name or args
  - By index:    forget(indices=0)             — 0-based index in last tool batch
  - Multi-index: forget(indices=[0, 1])        — delete first 2 calls in last batch

The tool returns a special sentinel ``__FORGET__:…`` that tool_loop.py
detects and uses to mutate the messages list in-place.
"""

from __future__ import annotations

import json
from typing import Any

from nanobot.tools.base import Tool


class ForgetTool(Tool):
    """Delete previous tool call results from the conversation context."""

    # Shared context dict — tool_loop writes ``last_tool_calls`` before
    # each iteration's tool execution, and this tool reads it.
    _shared: dict[str, Any] = {}

    @classmethod
    def set_shared(cls, key: str, value: Any) -> None:
        cls._shared[key] = value

    @property
    def name(self) -> str:
        return "forget"

    @property
    def description(self) -> str:
        return (
            "Delete previous tool call results from context. "
            "By keyword: forget(keywords='curl xxx') matches tool name/args. "
            "By index: forget(indices=0) deletes the 1st tool call in the last batch; "
            "forget(indices=[0,1]) deletes the first 2. "
            "Deleted results are permanently removed from context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": (
                        "Keyword(s) to match against previous tool names and arguments. "
                        "e.g. 'memory_palace', 'curl xxx'. All matching tool call+result "
                        "pairs will be deleted."
                    ),
                },
                "indices": {
                    "anyOf": [
                        {"type": "integer", "minimum": 0},
                        {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 0},
                        },
                    ],
                    "description": (
                        "Index(es) of tool calls in the last batch to delete. "
                        "0-based. e.g. 0 deletes the first call, [0,1] deletes "
                        "the first two."
                    ),
                },
            },
        }

    async def execute(self, *, keywords: str | None = None, indices: int | list[int] | None = None) -> str:
        # Prefer prev_tool_calls (the batch BEFORE the current one, which
        # contains the calls the user actually wants to forget).  Fall back
        # to last_tool_calls for backward compatibility.
        last_calls: list[dict] = self._shared.get("prev_tool_calls") or self._shared.get("last_tool_calls", [])
        forgot_ids: set[str] = self._shared.get("_forgot_ids", set())



        if not last_calls:
            return "No previous tool calls found to delete."

        to_delete: list[dict] = []

        if keywords is not None:
            kw_lower = keywords.lower()
            for tc in last_calls:
                # Skip already-forgot entries
                if tc.get("tool_call_id") in forgot_ids:
                    continue
                name = tc.get("name", "").lower()
                args_str = tc.get("args_str", "").lower()
                if kw_lower in name or kw_lower in args_str:
                    to_delete.append(tc)

        if indices is not None:
            if isinstance(indices, str):
                indices = json.loads(indices)
            if isinstance(indices, int):
                indices = [indices]
            for idx in indices:
                idx = int(idx)  # guard against string elements from serialization
                if 0 <= idx < len(last_calls):
                    tc = last_calls[idx]
                    # Skip already-forgot entries
                    if tc.get("tool_call_id") in forgot_ids:
                        continue
                    if tc not in to_delete:
                        to_delete.append(tc)

        if not to_delete:
            available = [
                f"  [{i}] {tc.get('name', '?')}({tc.get('args_str', '')[:80]})"
                for i, tc in enumerate(last_calls)
            ]
            return (
                "No matching tool calls found (already-forgot entries are excluded). Last batch:\n"
                + "\n".join(available)
            )

        # Build the deletion plan as JSON for tool_loop to process
        delete_ids = [tc.get("tool_call_id") for tc in to_delete]
        # Also include the assistant message index that contains these tool_calls
        # so tool_loop can remove the tool_calls entry from it
        delete_info = {
            "tool_call_ids": delete_ids,
            "names": [tc.get("name", "?") for tc in to_delete],
        }

        # Return sentinel — tool_loop.py detects this and mutates messages
        return f"__FORGET__:{json.dumps(delete_info, ensure_ascii=False)}"
