"""Streaming display helpers for group chat.

Provides a reusable `StreamingDisplay` class that encapsulates the common
pattern of building streaming messages, throttling edits, and handling
the final message update. Eliminates duplication across direct_chat,"""

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
        *,
        placeholder_on_start: bool = False,
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
        # Partial text already streamed to the pre-tool message. Preserved so
        # on_reset / finalize can keep it visible (with a tool marker) rather
        # than overwriting the in-progress reply with a bare "🔧 ..." icon —
        # which made the user's streamed text appear to vanish mid-stream.
        self._pre_tool_partial: str = ""
        self._placeholder_on_start = placeholder_on_start
        self._placeholder_active = False

    @property
    def enabled(self) -> bool:
        """Whether streaming is possible (both send and edit callbacks set)."""
        return bool(self._send_and_get_id and self._edit)

    async def ensure_started(self) -> None:
        """Post a TTFT placeholder before first delta (no config)."""
        if not self._placeholder_on_start:
            return
        if self.msg_id is not None or not self._send_and_get_id:
            return
        try:
            text = f"{self.header}▍ …"
            self.msg_id = await self._send_and_get_id(text)
            self._placeholder_active = True
            self._last_edit = time.time()
        except Exception as e:
            logger.debug("StreamingDisplay ensure_started failed: {}", e)

    @property
    def buffer_text(self) -> str:
        """Current accumulated text in the buffer."""
        return "".join(self._buffer)

    async def on_delta(self, delta: str) -> None:
        """Content delta callback — accumulate and periodically edit."""
        self._buffer.append(delta)
        now = time.time()
        body = self.header + "".join(self._buffer) + " ▍"

        if self.msg_id is None and self._send_and_get_id:
            self.msg_id = await self._send_and_get_id(body)
            self._placeholder_active = False
            self._last_edit = now
            return

        if self.msg_id and self._edit:
            if self._placeholder_active:
                try:
                    await self._edit(self.msg_id, body)
                except Exception:
                    pass
                self._placeholder_active = False
                self._last_edit = now
                return
            if (now - self._last_edit) >= self.EDIT_INTERVAL:
                try:
                    await self._edit(self.msg_id, body)
                except Exception:
                    pass
                self._last_edit = now

    async def on_reset(self) -> None:
        """Reset callback — tool calls interrupt mid-stream.

        Preserves the partial text already streamed in the current message
        (appending a tool-in-progress marker) instead of overwriting it with
        a bare "🔧 ..." placeholder — so the user does not see their
        in-progress reply vanish when the agent calls a tool. The buffer is
        cleared so post-tool content creates a NEW message below the
        tool-call messages, preserving chronological display order.

        (``result.content`` returned by tool_loop holds only the final
        post-tool text response, not the pre-tool prelude; keeping the
        prelude visible here is what prevents it being lost from display.)
        """
        partial = self.buffer_text
        self._buffer.clear()
        if self.msg_id and self._edit:
            if partial.strip():
                text = f"{self.header}{partial}\n\n🔧 ⏳"[:4096]
            else:
                text = self._tool_in_progress_text
            try:
                await self._edit(self.msg_id, text)
            except Exception:
                pass
            # Abandon old message — next delta creates a new one below tools.
            self._pre_tool_msg_id = self.msg_id
            self._pre_tool_partial = partial
            self.msg_id = None

    async def abort(self, *, reason: str = "⏹ 已中断") -> None:
        """Clean up an in-progress stream when the task is cancelled (/stop)."""
        partial = self.buffer_text.strip()
        if self._pre_tool_msg_id and self._edit:
            try:
                marker = (
                    f"{self.header}{self._pre_tool_partial}\n\n↓"[:4096]
                    if self._pre_tool_partial.strip()
                    else f"{self.header}↓"
                )
                await self._edit(self._pre_tool_msg_id, marker)
            except Exception:
                pass
            self._pre_tool_msg_id = None
            self._pre_tool_partial = ""

        if self.msg_id and self._edit:
            if partial:
                text = f"{self.header}{partial}\n\n{reason}"[:4096]
            else:
                text = f"{self.header}{reason}"[:4096]
            try:
                await self._edit(self.msg_id, text)
            except Exception:
                pass

        self.msg_id = None
        self._buffer.clear()

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
        # Clean up stale pre-tool message if it exists. Keep the prelude text
        # visible (with a "continued below" marker) rather than collapsing it
        # to a bare "↓" — the prelude is not part of result.content, so
        # hiding it here would lose it from display.
        if self._pre_tool_msg_id and self._edit:
            try:
                marker = (
                    f"{self.header}{self._pre_tool_partial}\n\n↓"[:4096]
                    if self._pre_tool_partial.strip()
                    else f"{self.header}↓"
                )
                await self._edit(self._pre_tool_msg_id, marker)
            except Exception:
                pass
            self._pre_tool_msg_id = None
            self._pre_tool_partial = ""

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

