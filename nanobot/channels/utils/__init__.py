"""Shared utilities for chat channel implementations.

This package provides reusable components for:
- Message deduplication (MessageDeduper)
- Media downloading (MediaDownloader)
- Message splitting (MessageSplitter)

These utilities reduce code duplication across channels (Feishu, Discord, Matrix, etc.)
"""

from nanobot.channels.utils.dedup import MessageDeduper
from nanobot.channels.utils.media import MediaDownloader
from nanobot.channels.utils.message import MessageSplitter

__all__ = ["MessageDeduper", "MediaDownloader", "MessageSplitter"]
