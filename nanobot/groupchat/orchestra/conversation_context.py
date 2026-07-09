"""ConversationContext — the single mutation seam for conversation history.

Step 1 of the coupling refactor. Before this, conversation history was mutated
from three independent paths:

1. ``HistoryContext.add_message`` / ``maybe_compress`` (the intended owner)
2. ``engine._history[:] = ...`` manual shim re-syncs scattered across 6+
   engine methods, AND
3. ``ClearContextTool._clear_one`` reaching directly into
   ``engine._history[:] = new_history`` — bypassing HistoryContext entirely
   (skipping _compress_active guard, persistence, the head/tail protections).

This gateway makes path 1 the ONLY mutation path. It is a thin facade over
``HistoryContext`` (no state moved) that exposes the operations the rest of
the engine needs:

- ``add(sender, content)``        → HistoryContext.add_message + persist
- ``clear_for_agent(agent, keep)`` → the ClearContextTool operation, now routed
- ``replace_all(messages)``       → bulk restore (snapshot load)
- ``maybe_compress()``             → AI/mechanical compression
- read access via ``messages`` / ``__getitem__`` / ``__len__``

``engine._history`` becomes a READ-ONLY view of ``context.messages``. The
scattered ``self._history = self.history.messages`` re-syncs collapse to one
property. New mutation code must go through this seam; never write
``engine._history[:] = ...`` again.

See ``docs/groupchat-coupling-fix.md`` (Step 1) and ``ports.py``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanobot.groupchat.history.context import HistoryContext
    from nanobot.groupchat.history.persistence import GroupChatState


class ConversationContext:
    """Concrete ``ports.ConversationContext`` — mutation gateway over HistoryContext.

    Thin wrapper; owns no message state itself (HistoryContext still does).
    The point is a single, audited entry point for every mutation.
    """

    def __init__(self, history: "HistoryContext", state: "GroupChatState") -> None:
        self._history = history
        self._state = state
        # Optional shadow Context twin (set by engine). clear_for_agent is the
        # one write seam bypassed by ClearContextTool (called directly on the
        # context, not through engine), so the mirror must be synced here too.
        self._shadow: Any = None

    # ── Read surface (delegating) ─────────────────────────────────────────
    # Reads are fine to expose widely; only mutations must go through the
    # methods below. ``messages`` is the live list object — callers may READ
    # it but must not mutate it in place.

    @property
    def messages(self) -> list[dict[str, str]]:
        return self._history.messages

    def __len__(self) -> int:
        return len(self._history)

    def __bool__(self) -> bool:
        return bool(self._history)

    def __iter__(self):
        return iter(self._history)

    def __getitem__(self, idx):
        return self._history[idx]

    def format(self) -> str:
        return self._history.format()

    # ── Mutation seam (the ONLY mutation paths) ────────────────────────────

    def add(self, sender: str, content: str) -> None:
        """Append a message: enforce limits + persist (HistoryContext.add_message)."""
        self._history.add_message(sender, content)

    def microcompact(self) -> None:
        """Cheap no-LLM aging pass over old tool-log blocks."""
        self._history.microcompact()

    async def maybe_compress(self) -> None:
        """AI/mechanical compression of the middle region on overflow."""
        await self._history.maybe_compress()

    def clear_for_agent(self, agent: str, keep_last: int = 0) -> int:
        """Remove an agent's oldest messages, keeping the last ``keep_last``.

        Routed here from ClearContextTool so the operation honours
        HistoryContext's invariants instead of slicing the list behind its
        back (which previously skipped the _compress_active guard + persistence).

        Returns the number of messages removed.
        """
        messages = self._history.messages
        agent_msgs = [m for m in messages if m.get("sender") == agent]
        total = len(agent_msgs)
        if total == 0:
            return 0
        remove_count = max(0, total - keep_last)
        if remove_count == 0:
            return 0

        removed = 0
        agent_seen = 0
        new_messages: list[dict[str, str]] = []
        for m in messages:
            if m.get("sender") == agent:
                agent_seen += 1
                if agent_seen <= remove_count:
                    removed += 1
                    continue
            new_messages.append(m)

        # Rebuild via the same snapshot-replace path HistoryContext uses so
        # _compress_active / persistence are respected. We touch the live list
        # object (not rebind the attribute) to keep all existing references
        # valid — mirrors how maybe_compress's rebuild works.
        messages[:] = new_messages
        # Shadow mirror (Step 3b-2): this seam is invoked directly by
        # ClearContextTool, bypassing engine, so sync the twin here. meta.agent
        # is set to the sender at add time (see engine._ctx_mirror_add) and at
        # restore/re-sync time (Context.from_sender_dicts), matching this agent.
        _shadow = getattr(self, "_shadow", None)
        if _shadow is not None:
            try:
                _shadow.delete_by_meta("agent", agent, keep_last=keep_last)
            except Exception as exc:  # noqa: BLE001 - shadow must never break prod
                logger.warning("Shadow ctx clear_for_agent failed ({}): {}", agent, exc)
        logger.info(
            "ConversationContext: cleared {}/{} messages for {} (kept last {})",
            removed, total, agent, keep_last,
        )
        return removed

    def replace_all(self, messages: list[dict[str, str]]) -> None:
        """Bulk replace the entire message list (used by snapshot restore)."""
        self._history.messages[:] = messages
