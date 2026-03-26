"""Two-phase context pruning for tool results.

Inspired by OpenClaw's context-pruning strategy, this module prunes old
tool-result messages to keep the prompt within budget while preserving
prefix stability for prompt caching.

Phase 1 — **Soft Trim**: When estimated tokens exceed ``soft_ratio`` of the
context window, old tool results are truncated to ``head_chars + tail_chars``
(e.g. first 1500 + last 1500 chars).

Phase 2 — **Hard Clear**: When estimated tokens still exceed ``hard_ratio``,
old tool results are replaced with a short placeholder string.

Only tool-result messages *before* the last ``keep_recent`` assistant turns
are eligible for pruning.  User and assistant messages are never modified.

Usage::

    from nanobot.agent.context_pruning import prune_messages
    messages = prune_messages(messages, context_window_tokens=200_000)
"""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN = 4  # conservative estimate

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_SOFT_RATIO = 0.3     # start soft trim when > 30% of window
DEFAULT_HARD_RATIO = 0.5     # start hard clear when > 50% of window
DEFAULT_KEEP_RECENT = 3      # protect last N assistant turns
DEFAULT_SOFT_MAX_CHARS = 4_000
DEFAULT_SOFT_HEAD_CHARS = 1_500
DEFAULT_SOFT_TAIL_CHARS = 1_500
DEFAULT_HARD_PLACEHOLDER = "[Old tool result cleared]"


# ── Helpers ───────────────────────────────────────────────────────────────

def _estimate_message_chars(msg: dict[str, Any]) -> int:
    """Estimate character count for a single message."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += len(block.get("text", ""))
                elif block.get("type") == "image_url":
                    total += 8_000  # image estimate
        return total
    return 0


def _estimate_total_chars(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_message_chars(m) for m in messages)


def _find_cutoff_index(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """Find index before which tool results can be pruned.

    Returns the index of the Nth-from-last assistant message.
    Everything before this index is eligible for pruning.
    """
    if keep_recent <= 0:
        return len(messages)

    count = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            count += 1
            if count >= keep_recent:
                return i
    return 0  # not enough assistant messages, don't prune


def _soft_trim_content(content: str, head_chars: int, tail_chars: int) -> str:
    """Trim long content to head + tail with a marker in between."""
    if len(content) <= head_chars + tail_chars + 100:
        return content
    head = content[:head_chars]
    tail = content[-tail_chars:] if tail_chars > 0 else ""
    trimmed_chars = len(content) - head_chars - tail_chars
    return f"{head}\n...\n[trimmed {trimmed_chars:,} chars]\n...\n{tail}"


# ── Main function ─────────────────────────────────────────────────────────

def prune_messages(
    messages: list[dict[str, Any]],
    context_window_tokens: int,
    *,
    soft_ratio: float = DEFAULT_SOFT_RATIO,
    hard_ratio: float = DEFAULT_HARD_RATIO,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    soft_max_chars: int = DEFAULT_SOFT_MAX_CHARS,
    soft_head_chars: int = DEFAULT_SOFT_HEAD_CHARS,
    soft_tail_chars: int = DEFAULT_SOFT_TAIL_CHARS,
    hard_placeholder: str = DEFAULT_HARD_PLACEHOLDER,
) -> list[dict[str, Any]]:
    """Prune old tool-result messages to fit within the context window.

    Returns a new list (shallow copy) with pruned messages.
    User/assistant messages are never modified.
    Only tool results before the last ``keep_recent`` assistant turns are pruned.

    Args:
        messages: The full message list.
        context_window_tokens: Model's context window size in tokens.
        soft_ratio: Trigger soft trim when chars exceed this fraction of window.
        hard_ratio: Trigger hard clear when chars exceed this fraction of window.
        keep_recent: Number of recent assistant turns to protect from pruning.

    Returns:
        Pruned message list (may be the same object if no pruning needed).
    """
    if not messages or context_window_tokens <= 0:
        return messages

    char_window = context_window_tokens * CHARS_PER_TOKEN
    total_chars = _estimate_total_chars(messages)
    ratio = total_chars / char_window

    # Below soft threshold — no pruning needed
    if ratio < soft_ratio:
        return messages

    cutoff = _find_cutoff_index(messages, keep_recent)
    if cutoff <= 0:
        return messages  # nothing to prune

    # Collect prunable tool result indices (before cutoff)
    prunable: list[int] = []
    for i in range(cutoff):
        if messages[i].get("role") == "tool":
            content = messages[i].get("content", "")
            if isinstance(content, str) and len(content) > soft_max_chars:
                prunable.append(i)

    if not prunable:
        return messages

    # Phase 1: Soft Trim
    result = list(messages)  # shallow copy
    for i in prunable:
        content = result[i].get("content", "")
        if not isinstance(content, str):
            continue
        if len(content) <= soft_max_chars:
            continue
        trimmed = _soft_trim_content(content, soft_head_chars, soft_tail_chars)
        result[i] = {**result[i], "content": trimmed}
        total_chars += len(trimmed) - len(content)

    ratio = total_chars / char_window
    if ratio < hard_ratio:
        return result

    # Phase 2: Hard Clear (oldest first)
    for i in prunable:
        if ratio < hard_ratio:
            break
        content = result[i].get("content", "")
        if not isinstance(content, str):
            continue
        old_len = len(content)
        if old_len <= len(hard_placeholder) + 100:
            continue  # already small enough
        result[i] = {**result[i], "content": hard_placeholder}
        total_chars += len(hard_placeholder) - old_len
        ratio = total_chars / char_window

    return result
