"""Message splitting utilities for chat channels.

Different platforms have different message length limits. This module provides
intelligent message splitting that preserves markdown formatting and code blocks.
"""

from __future__ import annotations

from typing import Literal


class MessageSplitter:
    """Platform-aware message splitting with markdown preservation.

    Splits long messages into chunks that fit within platform limits,
    attempting to preserve markdown structure (code blocks, lists, etc.).

    Example:
        splitter = MessageSplitter("telegram")
        chunks = splitter.split(long_message)
        for chunk in chunks:
            await send(chunk)
    """

    PLATFORM_LIMITS: dict[str, int] = {
        "telegram": 4096,
        "discord": 2000,
        "slack": 4000,
        "matrix": 16384,
        "feishu": 30000,
        "whatsapp": 4096,
        "email": 500000,  # Practical limit for email
    }

    def __init__(
        self,
        platform: str,
        limit: int | None = None,
    ):
        """Initialize the message splitter.

        Args:
            platform: Platform name (used to look up default limit).
            limit: Optional override for message length limit.
        """
        self._platform = platform
        self._limit = limit or self.PLATFORM_LIMITS.get(platform, 4096)

    @property
    def limit(self) -> int:
        """The maximum message length for this platform."""
        return self._limit

    def split(self, text: str) -> list[str]:
        """Split text into chunks that fit within the limit.

        Attempts to split at natural boundaries (paragraphs, newlines)
        while preserving markdown formatting.

        Args:
            text: The text to split.

        Returns:
            List of text chunks, each within the limit.
        """
        if len(text) <= self._limit:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= self._limit:
                chunks.append(remaining)
                break

            # Try to find a good split point
            split_point = self._find_split_point(remaining)

            chunk = remaining[:split_point]
            chunks.append(chunk)
            remaining = remaining[split_point:]

        return chunks

    def _find_split_point(self, text: str) -> int:
        """Find the best point to split text.

        Prefers splitting at paragraph boundaries, then newlines,
        then spaces, falling back to hard split.
        """
        # Look for paragraph break within limit
        search_region = text[: self._limit]
        para_break = search_region.rfind("\n\n")
        if para_break > self._limit // 2:
            return para_break + 2

        # Look for newline
        newline = search_region.rfind("\n")
        if newline > self._limit // 2:
            return newline + 1

        # Look for space
        space = search_region.rfind(" ")
        if space > self._limit // 2:
            return space + 1

        # Hard split at limit
        return self._limit

    def split_preserve_code_blocks(self, text: str) -> list[str]:
        """Split text while preserving code block integrity.

        Code blocks (```) are kept together when possible.

        Args:
            text: The text to split.

        Returns:
            List of text chunks with balanced code blocks.
        """
        if len(text) <= self._limit:
            return [text]

        # Count code block markers
        code_markers = text.count("```")
        if code_markers % 2 != 0:
            # Odd number of markers - don't try to preserve
            return self.split(text)

        # Simple approach: split, then fix code blocks
        raw_chunks = self.split(text)
        result: list[str] = []
        in_code_block = False

        for chunk in raw_chunks:
            # Check if we're starting in a code block
            if in_code_block:
                chunk = "```\n" + chunk

            # Count markers in this chunk
            markers_in_chunk = chunk.count("```")
            in_code_block = (in_code_block != (markers_in_chunk % 2 == 1))

            # Close code block if needed
            if in_code_block and len(chunk) + 4 <= self._limit:
                chunk = chunk + "\n```"
                in_code_block = False

            result.append(chunk)

        return result
