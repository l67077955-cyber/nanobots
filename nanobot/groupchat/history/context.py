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

from nanobot.groupchat.history.message_converter import age_tool_log

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
    def _find_head_indices(history: list[dict], keep_all_users: bool = False) -> set[int]:
        """Return indices of head-protected messages.

        Always protects index 0 (system prompt).  If *keep_all_users* is True,
        protects **all** user messages; otherwise only the first user message.
        """
        protected = {0}
        for i, msg in enumerate(history):
            if msg.get("sender") in ("User", "user", "用户"):
                protected.add(i)
                if not keep_all_users:
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
                keep_user_messages,
            )
            limit = max_messages()
            char_budget = max_context_chars()
            _keep_users = keep_user_messages()
        except Exception:
            limit = 150
            char_budget = 0
            _keep_users = False

        # ── Pre-identify protected head before any trimming ──
        head_indices = self._find_head_indices(self.messages, keep_all_users=_keep_users)
        head_msgs = [self.messages[i] for i in sorted(head_indices)]

        # Step 1: message-count limit — keep most-recent N, always keep head
        if len(self.messages) > limit:
            tail = self.messages[-limit:]
            tail_ids = {id(m) for m in tail}
            extra_head = [m for m in head_msgs if id(m) not in tail_ids]
            self.messages = extra_head + tail

        # Step 2: char-budget trimming — head is counted but always kept
        if char_budget > 0:
            head_chars = sum(len(m.get("content", "")) for m in head_msgs)
            available = max(0, char_budget - head_chars)
            head_id_set = {id(m) for m in head_msgs}
            tail: list[dict] = []
            for m in reversed(self.messages):
                if id(m) in head_id_set:
                    continue
                c = len(m.get("content", ""))
                if available - c < 0:
                    break
                tail.insert(0, m)
                available -= c
            self.messages = head_msgs + tail

        self._state.save_message(sender, content, self.messages)

    async def maybe_compress(self) -> None:
        """Compress the middle section of history when it approaches the limit.

        Head-tail protection always runs.  AI summarisation is gated by
        ``summarize_enabled``; if disabled, the middle region is simply dropped.
        """
        from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
            max_messages,
            history_summarize_enabled,
            summarize_model,
            compress_ratio,
            compress_max_summary_tokens,
            compression_keep_recent,
            keep_user_messages,
        )

        limit = max_messages()
        ratio = compress_ratio()
        if len(self.messages) < int(limit * ratio):
            return

        total_len = len(self.messages)

        # ── 1. Protected Head ──
        protected_head_indices = self._find_head_indices(
            self.messages, keep_all_users=keep_user_messages()
        )

        # ── 2. Protected Tail ──
        keep_recent = compression_keep_recent()
        protected_tail_indices = set(
            range(max(0, total_len - keep_recent), total_len)
        )

        # ── 3. Compressible Middle ──
        # Use set exclusion instead of contiguous range — head indices may be
        # non-contiguous when keep_user_messages=True (user messages scattered).
        all_protected = protected_head_indices | protected_tail_indices

        head = [self.messages[i] for i in sorted(protected_head_indices)]
        tail = [
            self.messages[i]
            for i in sorted(protected_tail_indices)
            if i not in protected_head_indices
        ]
        to_compress = [
            self.messages[i] for i in range(total_len) if i not in all_protected
        ]

        if not to_compress:
            return

        # ── 3.5 Age tool logs in middle region before summarisation ──
        # Strip verbose previews from tool call logs to reduce summary input
        # size and preserve more semantic content in the compressed output.
        for msg in to_compress:
            original = msg["content"]
            aged = age_tool_log(original)
            if aged != original:
                msg["content"] = aged

        # ── 4a. AI Summarise ──
        if history_summarize_enabled() and self._provider is not None:
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
        self.messages = head + tail
        logger.info(
            "HistoryContext: dropped {} middle messages (head: {}, tail: {})",
            len(to_compress),
            len(head),
            len(tail),
        )
