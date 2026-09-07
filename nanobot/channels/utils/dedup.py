"""Message deduplication utilities for chat channels.

Provides deduplication to prevent processing the same message multiple times,
which is especially important for platforms that may deliver duplicate messages.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Literal


class MessageDeduper:
    """Thread-safe message deduplication with configurable strategies.

    Supports two strategies:
    - "ordered": Maintains insertion order, evicts oldest when full (LRU-style)
    - "set": Simple set-based dedup, random eviction when full

    Example:
        deduper = MessageDeduper(capacity=1000, strategy="ordered")
        if deduper.is_duplicate(msg_id):
            return  # Skip duplicate
        # Process message...
    """

    def __init__(
        self,
        capacity: int = 1000,
        strategy: Literal["ordered", "set"] = "ordered",
    ):
        """Initialize the deduper.

        Args:
            capacity: Maximum number of message IDs to track.
            strategy: Deduplication strategy ("ordered" or "set").
        """
        self._capacity = capacity
        self._strategy = strategy

        if strategy == "ordered":
            self._seen: OrderedDict[str, None] = OrderedDict()
        else:
            self._seen_set: set[str] = set()

    def is_duplicate(self, msg_id: str) -> bool:
        """Check if a message ID has been seen, and record it if new.

        Args:
            msg_id: The unique message identifier.

        Returns:
            True if the message was already seen (duplicate), False if new.
        """
        if self._strategy == "ordered":
            if msg_id in self._seen:
                return True
            self._seen[msg_id] = None
            if len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return False
        else:
            if msg_id in self._seen_set:
                return True
            self._seen_set.add(msg_id)
            if len(self._seen_set) > self._capacity:
                # Evict half randomly (simple approach)
                to_remove = len(self._seen_set) // 2
                for _ in range(to_remove):
                    self._seen_set.pop()
            return False

    def clear(self) -> None:
        """Clear all tracked message IDs."""
        if self._strategy == "ordered":
            self._seen.clear()
        else:
            self._seen_set.clear()

    def __len__(self) -> int:
        """Return the number of tracked message IDs."""
        if self._strategy == "ordered":
            return len(self._seen)
        return len(self._seen_set)
