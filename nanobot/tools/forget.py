"""Forget tool — delete previous tool call results from context.

Allows the agent (or groupchat participants) to selectively remove
tool call + result pairs from the conversation history so they don't
consume tokens or influence future LLM calls.

The tool returns a special sentinel string starting with ``__FORGET__:``
that tool_loop detects and uses to mutate the messages list in-place.
"""

from __future__ import annotations

import json
from typing import Any

from nanobot.tools.base import Tool


class ForgetTool(Tool):
    """Delete previous tool call results from the conversation context."""

    # Instance-level context avoids cross-agent pollution in groupchat.
    # tool_loop injects per-iteration state via _ctx before executing tools.
    def __init__(self, context: dict[str, Any] | None = None):
        self._ctx: dict[str, Any] = context or {}

    @classmethod
    def set_context(cls, tool: "ForgetTool", ctx: dict[str, Any]) -> None:
        if isinstance(tool, ForgetTool):
            tool._ctx = ctx

    # Backward-compat shim for callers still using the old class-level dict.
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
            "Delete previous tool call results from context so they no longer "
            "consume tokens or influence future answers. "
            "Use by keyword (matches tool name or args) or by 0-based index "
            "into the previous tool batch. "
            "Deleted results are permanently removed from the active history."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": (
                        "Keyword(s) to match against previous tool names and "
                        "argument strings. Example: 'memory_palace' or 'curl xxx'. "
                        "All matching tool_call + result pairs will be deleted."
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
                        "0-based index (or list of indices) into the last tool "
                        "batch to delete. Example: 0 deletes the first call in the "
                        "previous batch; [0, 2] deletes the 1st and 3rd."
                    ),
                },
            },
        }

    def _active_ctx(self) -> dict[str, Any]:
        return self._ctx if self._ctx else self._shared

    async def execute(
        self, *, keywords: str | None = None, indices: int | list[int] | None = None
    ) -> str:
        ctx = self._active_ctx()
        last_calls: list[dict] = (
            ctx.get("target_tool_calls")
            or ctx.get("prev_tool_calls")
            or ctx.get("last_tool_calls", [])
        )
        forgot_ids: set[str] = ctx.setdefault("_forgot_ids", set())

        if not last_calls:
            return "No previous tool calls found to delete."

        to_delete: list[dict] = []

        if keywords is not None:
            kw_lower = keywords.lower()
            for tc in last_calls:
                if tc.get("tool_call_id") in forgot_ids:
                    continue
                name = (tc.get("name") or "").lower()
                args_str = (tc.get("args_str") or "").lower()
                if kw_lower in name or kw_lower in args_str:
                    to_delete.append(tc)

        if indices is not None:
            if isinstance(indices, str):
                try:
                    indices = json.loads(indices)
                except Exception:
                    indices = []
            if isinstance(indices, int):
                indices = [indices]
            if isinstance(indices, (list, tuple)):
                for raw_idx in indices:
                    try:
                        idx = int(raw_idx)
                    except Exception:
                        continue
                    if 0 <= idx < len(last_calls):
                        tc = last_calls[idx]
                        if tc.get("tool_call_id") in forgot_ids:
                            continue
                        if tc not in to_delete:
                            to_delete.append(tc)

        if not to_delete:
            available = [
                f"  [{i}] {tc.get('name', '?')}({(tc.get('args_str') or '')[:80]})"
                for i, tc in enumerate(last_calls)
            ]
            return (
                "No matching tool calls found (already-forgot entries are excluded). "
                "Last batch:\n" + "\n".join(available)
            )

        delete_ids = [tc.get("tool_call_id") for tc in to_delete if tc.get("tool_call_id")]
        delete_info = {
            "tool_call_ids": delete_ids,
            "names": [tc.get("name", "?") for tc in to_delete],
        }

        forgot_ids.update(delete_ids)

        return f"__FORGET__:{json.dumps(delete_info, ensure_ascii=False)}"
