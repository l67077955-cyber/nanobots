"""HistoryContext — single source of truth for conversation history.

Owns the message list and all related operations:
  - add_message     : append + enforce message-count & char-budget limits
  - maybe_compress  : AI summarize (or drop) the middle region on overflow
  - clear / format  : utility helpers

Engine and Broadcast delegate to this class instead of maintaining
``self._history`` directly.  Persistence is still delegated to
``GroupChatState.save_message()``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanobot.groupchat.history.persistence import GroupChatState


class HistoryContext:
    """Encapsulates the shared conversation history for a group chat session.

    Parameters
    ----------
    state:
        The ``GroupChatState`` persistence layer; used to persist each
        message and the current history snapshot to disk.
    provider:
        The LLM provider used for AI summarisation.  May be ``None`` when
        summarisation is disabled.
    """

    def __init__(
        self,
        state: "GroupChatState",
        provider: Any = None,
    ) -> None:
        self._state = state
        self._provider = provider
        self.messages: list[dict[str, str]] = []

    # ── Compatibility shim: allow engine._history to keep working ─────────
    # We expose the messages list directly as a public attribute so that
    # code still referencing ``engine._history`` can be updated gradually.
    # TODO: remove once all callers are migrated.

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _find_head_indices(history: list[dict]) -> set[int]:
        """Return indices of head-protected messages: index 0 + first user msg."""
        protected = {0}
        for i, msg in enumerate(history):
            if msg.get("sender") in ("User", "user", "用户"):
                protected.add(i)
                break
        return protected

    # ── Public API ────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.messages)

    def __bool__(self) -> bool:
        return bool(self.messages)

    def __iter__(self):
        return iter(self.messages)

    def __getitem__(self, idx):
        return self.messages[idx]

    def clear(self) -> None:
        """Wipe the entire history."""
        self.messages.clear()

    def format(self) -> str:
        """Format history as a single readable string."""
        return "\n\n".join(
            f"[{m['sender']}]: {m['content']}" for m in self.messages
        )

    def add_message(self, sender: str, content: str) -> None:
        """Append a message and enforce message-count / char-budget limits.

        Head-protection guarantees that the very first message and the first
        user message are never evicted during trimming.
        """
        self.messages.append({"sender": sender, "content": content})

        try:
            from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
                max_messages,
                max_context_chars,
            )
            limit = max_messages()
            char_budget = max_context_chars()
        except Exception:
            limit = 150
            char_budget = 0

        # ── Pre-identify protected head before any trimming ──
        head_indices = self._find_head_indices(self.messages)
        head_msgs = [self.messages[i] for i in sorted(head_indices)]

        # Step 1: message-count limit — keep most-recent N, always keep head
        if len(self.messages) > limit:
            tail = self.messages[-limit:]
            tail_ids = {id(m) for m in tail}
            extra_head = [m for m in head_msgs if id(m) not in tail_ids]
            self.messages = extra_head + tail

        # Step 2: char-budget trimming — head is counted but always kept
        if char_budget > 0:
            head_indices = self._find_head_indices(self.messages)
            head_msgs = [self.messages[i] for i in sorted(head_indices)]
            head_chars = sum(len(m.get("content", "")) for m in head_msgs)
            available = max(0, char_budget - head_chars)

            tail: list[dict] = []
            head_id_set = {id(m) for m in head_msgs}
            for m in reversed(self.messages):
                if id(m) in head_id_set:
                    continue
                c = len(m.get("content", ""))
                if available - c < 0:
                    break
                tail.insert(0, m)
                available -= c

            # Rebuild: head first (preserving order), then tail
            seen: set[int] = set()
            rebuilt: list[dict] = []
            for m in head_msgs + tail:
                if id(m) not in seen:
                    rebuilt.append(m)
                    seen.add(id(m))
            self.messages = rebuilt

        self._state.save_message(sender, content, self.messages)

    async def maybe_compress(self) -> None:
        """Compress the middle section of history when it approaches the limit.

        Head-tail protection always runs.  AI summarisation is gated by
        ``summarize_enabled``; if disabled, the middle region is simply dropped.
        """
        from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
            max_messages,
            summarize_enabled,
            summarize_model,
            compress_ratio,
            compress_max_summary_tokens,
        )

        limit = max_messages()
        ratio = compress_ratio()
        if len(self.messages) < int(limit * ratio):
            return

        total_len = len(self.messages)

        # ── 1. Protected Head ──
        protected_head_indices = self._find_head_indices(self.messages)

        # ── 2. Protected Tail ──
        keep_recent = 6
        protected_tail_indices = set(
            range(max(0, total_len - keep_recent), total_len)
        )

        # ── 3. Compressible Middle ──
        compress_start = (
            max(protected_head_indices) + 1 if protected_head_indices else 0
        )
        compress_end = (
            min(protected_tail_indices) if protected_tail_indices else total_len
        )

        if compress_start >= compress_end:
            return

        to_compress = self.messages[compress_start:compress_end]
        if not to_compress:
            return

        head = [
            self.messages[i]
            for i in sorted(protected_head_indices)
            if i < compress_start
        ]
        tail = [
            self.messages[i]
            for i in sorted(protected_tail_indices)
            if i >= compress_end
        ]

        # ── 4a. AI Summarise ──
        if summarize_enabled() and self._provider is not None:
            history_text = "\n".join(
                f"[{m['sender']}]: {m['content']}" for m in to_compress
            )
            prompt = (
                f"以下是群聊的一段中期历史记录（共 {len(to_compress)} 条）。\n"
                "请用简洁的中文摘要这些内容，重点保留核心发现、关键决策、重要事实以及已经完成的进度。\n"
                "如果有具体的数值、文件路径或关键结论，请务必保留。\n"
                f"摘要不超过 500 字。\n\n{history_text}"
            )
            max_tok = compress_max_summary_tokens()
            try:
                response = await self._provider.chat_with_retry(
                    messages=[{"role": "user", "content": prompt}],
                    model=summarize_model(),
                    max_tokens=max_tok,
                )
                summary = (response.content or "").strip()
                if summary:
                    summary_msg = {
                        "sender": "系统",
                        "content": (
                            f"[早期对话摘要（压缩了 {len(to_compress)} 条中间消息）]\n"
                            + summary
                        ),
                    }
                    self.messages = head + [summary_msg] + tail
                    logger.info(
                        "HistoryContext: AI compressed {} middle → summary "
                        "(head: {}, tail: {})",
                        len(to_compress),
                        len(head),
                        len(tail),
                    )
                    return
            except Exception as e:
                logger.warning(
                    "HistoryContext: AI compress failed: {}, falling back to drop", e
                )

        # ── 4b. Fallback: drop middle region ──
        if not summarize_enabled():
            # If AI is disabled, we don't drop messages here; we let add_message's 
            # hard limits (max_messages) handle it to avoid "disappearing messages" 
            # that look like a bug.
            return

        self.messages = head + tail
        logger.info(
            "HistoryContext: dropped {} middle messages (head: {}, tail: {})",
            len(to_compress),
            len(head),
            len(tail),
        )
