"""Two-phase context pruning for tool results.

Inspired by OpenClaw's context-pruning strategy, this module prunes old
tool-result messages to keep the prompt within budget while preserving
prefix stability for prompt caching.

Phase 1 — **Soft Trim**: When estimated tokens exceed ``soft_ratio`` of the
context window, old tool results are truncated to ``head_chars + tail_chars``
(e.g. first 1500 + last 1500 chars).

Phase 2 — **Smart Clear**: When estimated tokens still exceed ``hard_ratio``,
old tool results are replaced with a compact fact-extraction placeholder that
preserves key structured information (paths, errors, URLs, numbers) so the
model retains critical context even after hard pruning.

Only tool-result messages *before* the last ``keep_recent`` assistant turns
are eligible for pruning.  User and assistant messages are never modified.

Usage::

    from nanobot.agent.context_pruning import prune_messages
    messages = prune_messages(messages, context_window_tokens=200_000)
"""

from __future__ import annotations

import re
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

# ── Key-fact extraction patterns ──────────────────────────────────────────

# File/system paths (Unix and Windows)
_RE_PATHS = re.compile(
    r'(?:^|[\s\'"`(])(/(?:[\w.\-]+/){1,}[\w.\-]+|(?:[A-Za-z]:)?\\(?:[\w.\-]+\\){1,}[\w.\-]+)',
    re.MULTILINE,
)
# Error / exception lines
_RE_ERROR_LINE = re.compile(
    r'^.{0,120}(?:error|exception|traceback|errno|failed|failure|fatal|critical).{0,120}$',
    re.IGNORECASE | re.MULTILINE,
)
# URLs
_RE_URLS = re.compile(r'https?://\S{8,}')
# Important key=value or key: value pairs (exit code, version, port, count…)
_RE_KV = re.compile(
    r'\b(exit[_ ]?code|returncode|status|version|port|host|ip|id|count|total|size|pid|result)\s*[:=]\s*(\S{1,60})',
    re.IGNORECASE,
)
# Standalone numbers that look significant (not inside long words)
_RE_NUMBERS = re.compile(r'(?<!\w)(\d{1,10})(?!\w)')


def _extract_key_facts(content: str, tool_name: str = "") -> str:
    """Extract key structured facts from tool output for a compact placeholder.

    Returns a string like:
        [Cleared 12345c | paths:/foo/bar,/etc/app.conf | errors:connection refused | urls:https://... | kv:port=8080,status=200]

    Falls back to ``[Cleared Nc]`` if nothing noteworthy is found.
    """
    parts: list[str] = [f"Cleared {len(content):,}c"]

    # ── Paths ──
    raw_paths = [m.group(1).strip() for m in _RE_PATHS.finditer(content)]
    # deduplicate while preserving order
    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in raw_paths:
        if p not in seen and len(p) > 3:
            seen.add(p)
            unique_paths.append(p)
    if unique_paths:
        parts.append("paths:" + ",".join(unique_paths[:6]))

    # ── Error lines ──
    error_lines = [m.group(0).strip() for m in _RE_ERROR_LINE.finditer(content)]
    if error_lines:
        # keep shortest / most informative lines first
        error_lines.sort(key=len)
        parts.append("errors:" + " | ".join(error_lines[:3]))

    # ── URLs ──
    urls = list(dict.fromkeys(_RE_URLS.findall(content)))
    if urls:
        parts.append("urls:" + ",".join(urls[:3]))

    # ── Key-value pairs ──
    kvs = _RE_KV.findall(content)
    if kvs:
        seen_kv: set[str] = set()
        kv_parts: list[str] = []
        for k, v in kvs:
            key = k.lower().replace(" ", "_")
            entry = f"{key}={v}"
            if entry not in seen_kv:
                seen_kv.add(entry)
                kv_parts.append(entry)
        if kv_parts:
            parts.append("kv:" + ",".join(kv_parts[:6]))

    return "[" + " | ".join(parts) + "]"


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
    """Trim long content to head + extracted-middle-facts + tail.

    The middle section (chars head_chars..-tail_chars) is replaced with a
    compact fact-extraction summary instead of a blank ``[trimmed N chars]``
    marker, so key information (paths, errors, URLs, kv-pairs) buried in the
    middle is not silently discarded.
    """
    if len(content) <= head_chars + tail_chars + 100:
        return content
    head = content[:head_chars]
    tail = content[-tail_chars:] if tail_chars > 0 else ""
    middle = content[head_chars: len(content) - tail_chars]
    trimmed_chars = len(middle)
    middle_facts = _extract_key_facts(middle)
    return f"{head}\n...\n{middle_facts}\n...\n{tail}"


# ── Main function ─────────────────────────────────────────────────────────

def prune_messages(
    messages: list[dict[str, Any]],
    context_window_tokens: int,
    *,
    soft_ratio: float | None = None,
    hard_ratio: float | None = None,
    keep_recent: int | None = None,
    soft_max_chars: int | None = None,
    soft_head_chars: int | None = None,
    soft_tail_chars: int | None = None,
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
    # Resolve defaults from history_settings when not explicitly provided
    try:
        from nanobot.groupchat import history_settings as hs
        if soft_ratio is None:
            soft_ratio = hs.pruning_soft_ratio()
        if hard_ratio is None:
            hard_ratio = hs.pruning_hard_ratio()
        if keep_recent is None:
            keep_recent = hs.pruning_keep_recent()
        if soft_max_chars is None:
            soft_max_chars = hs.pruning_soft_max_chars()
        if soft_head_chars is None:
            soft_head_chars = hs.pruning_soft_head_chars()
        if soft_tail_chars is None:
            soft_tail_chars = hs.pruning_soft_tail_chars()
    except Exception:
        pass
    # Final fallback to module-level defaults
    if soft_ratio is None:
        soft_ratio = DEFAULT_SOFT_RATIO
    if hard_ratio is None:
        hard_ratio = DEFAULT_HARD_RATIO
    if keep_recent is None:
        keep_recent = DEFAULT_KEEP_RECENT
    if soft_max_chars is None:
        soft_max_chars = DEFAULT_SOFT_MAX_CHARS
    if soft_head_chars is None:
        soft_head_chars = DEFAULT_SOFT_HEAD_CHARS
    if soft_tail_chars is None:
        soft_tail_chars = DEFAULT_SOFT_TAIL_CHARS

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

    # Phase 2: Smart Clear (oldest first)
    # Instead of a blank placeholder, extract key facts so the model retains
    # critical structured information (paths, errors, URLs, numbers) even after
    # hard pruning.  Falls back to hard_placeholder only if extraction yields
    # a string longer than the original (shouldn't happen in practice).
    tool_name_map = _build_tool_name_map(messages)
    for i in prunable:
        if ratio < hard_ratio:
            break
        content = result[i].get("content", "")
        if not isinstance(content, str):
            continue
        old_len = len(content)
        if old_len <= len(hard_placeholder) + 100:
            continue  # already small enough
        tname = tool_name_map.get(i, "")
        compact = _extract_key_facts(content, tname)
        # Safety: if extraction somehow made it longer, fall back to blank placeholder
        replacement = compact if len(compact) < old_len else hard_placeholder
        result[i] = {**result[i], "content": replacement}
        total_chars += len(replacement) - old_len
        ratio = total_chars / char_window

    return result


def _build_tool_name_map(messages: list[dict[str, Any]]) -> dict[int, str]:
    """Map tool-result message indices to their tool name.

    The tool name is recovered from the preceding assistant message's
    ``tool_calls`` list by matching ``tool_call_id``.
    """
    # Build id → name from all assistant tool_calls
    id_to_name: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    tcid = tc.get("id", "")
                    name = fn.get("name", "")
                    if tcid and name:
                        id_to_name[tcid] = name

    result: dict[int, str] = {}
    for idx, msg in enumerate(messages):
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            result[idx] = id_to_name.get(tcid, "")
    return result
