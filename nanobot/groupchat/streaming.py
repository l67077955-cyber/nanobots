"""Streaming display helpers for group chat.

Provides a reusable `StreamingDisplay` class that encapsulates the common
pattern of building streaming messages, throttling edits, and handling
the final message update. Eliminates duplication across direct_chat,
_agent_speak, and orchestra._run_agent_grok.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from loguru import logger


class StreamingDisplay:
    """Manages a Telegram-style streaming message with throttled edits.

    Usage::

        stream = StreamingDisplay(header, send_and_get_id, edit_fn)
        # Pass stream.on_delta / stream.on_reset as LLM callbacks
        result = await tool_loop(..., on_content_delta=stream.on_delta, ...)
        await stream.finalize(content)  # final edit without cursor
    """

    EDIT_INTERVAL = 0.8  # seconds between throttled edits

    def __init__(
        self,
        header: str,
        send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None = None,
        edit_fn: Callable[[int, str], Awaitable[None]] | None = None,
        tool_in_progress_text: str | None = None,
    ) -> None:
        self.header = header
        self._send_and_get_id = send_and_get_id_fn
        self._edit = edit_fn
        self._tool_in_progress_text = tool_in_progress_text or f"{header}🔧 ..."
        self.msg_id: int | None = None
        self._buffer: list[str] = []
        self._last_edit: float = 0.0
        # Stale message ID from before a tool-call reset.
        # When tools interrupt streaming, we abandon the old message so
        # the final content appears *below* the tool-call messages.
        self._pre_tool_msg_id: int | None = None

    @property
    def enabled(self) -> bool:
        """Whether streaming is possible (both send and edit callbacks set)."""
        return bool(self._send_and_get_id and self._edit)

    async def on_delta(self, delta: str) -> None:
        """Content delta callback — accumulate and periodically edit."""
        self._buffer.append(delta)
        now = time.time()

        if self.msg_id is None and self._send_and_get_id:
            text = self.header + "".join(self._buffer) + " ▍"
            self.msg_id = await self._send_and_get_id(text)
            self._last_edit = now
        elif self.msg_id and self._edit and (now - self._last_edit) >= self.EDIT_INTERVAL:
            text = self.header + "".join(self._buffer) + " ▍"
            try:
                await self._edit(self.msg_id, text)
            except Exception:
                pass
            self._last_edit = now

    async def on_reset(self) -> None:
        """Reset callback — clear buffer when tool calls interrupt mid-stream.

        Abandons the current streaming message so the next content delta
        creates a NEW message below the tool-call messages, preserving
        chronological display order.
        """
        self._buffer.clear()
        if self.msg_id and self._edit:
            try:
                await self._edit(self.msg_id, self._tool_in_progress_text)
            except Exception:
                pass
            # Abandon old message — next delta creates new one below tools
            self._pre_tool_msg_id = self.msg_id
            self.msg_id = None

    async def finalize(
        self,
        content: str,
        fallback_send: Callable[[str], Awaitable[None]] | None = None,
        max_len: int = 4096,
    ) -> None:
        """Final edit — replace streaming cursor with final content.

        If content is empty, shows "(空回复)".
        Falls back to ``fallback_send`` if editing fails.
        """
        # Clean up stale pre-tool message if it exists
        if self._pre_tool_msg_id and self._edit:
            try:
                await self._edit(self._pre_tool_msg_id, f"{self.header}↓")
            except Exception:
                pass
            self._pre_tool_msg_id = None

        if content:
            final_text = f"{self.header}{content}"[:max_len]
            if self.msg_id and self._edit:
                try:
                    await self._edit(self.msg_id, final_text)
                    return
                except Exception as e:
                    logger.warning("StreamingDisplay finalize edit failed: {}", e)
            if fallback_send:
                await fallback_send(final_text)
        else:
            if self.msg_id and self._edit:
                try:
                    await self._edit(self.msg_id, f"{self.header}(空回复)")
                except Exception:
                    pass

