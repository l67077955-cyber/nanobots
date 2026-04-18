"""Prompt-cache hit-ratio estimator.

Analyses the ``cache_control`` breakpoints injected by ``_apply_cache_control``
and produces:

* A per-breakpoint breakdown (position, estimated token count, label)
* An overall **expected cache hit ratio** — the fraction of prompt tokens
  that are likely to be served from cache on the *next* call.

The estimate is intentionally cheap: we count characters and divide by 4
(Anthropic's rule-of-thumb for English/code; roughly correct for mixed
Chinese/English text too).

Anthropic's minimum cacheable block is **1 024 tokens** (≈ 4 096 chars).
Breakpoints below that threshold are flagged as "too small to cache".

Usage::

    from nanobot.providers.cache_probe import estimate_cache_ratio

    result = estimate_cache_ratio(messages_with_cache_control, tools_with_cc)
    logger.debug("cache probe: {}", result)
    # result.ratio_pct → e.g. 74 (percent)
    # result.summary   → "74% (bp0=sys 12k tok, bp1=hist 8k tok)"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Anthropic's minimum block size to actually be cached.
_MIN_CACHE_TOKENS = 1_024
_CHARS_PER_TOKEN = 4  # rule-of-thumb


def _content_chars(content: Any) -> int:
    """Return the character length of a message content value."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += len(block.get("text", "") or "")
        return total
    return len(str(content))


def _has_cache_control(content: Any) -> bool:
    """Return True if the content block carries a cache_control marker."""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                return True
    return False


@dataclass
class CacheBreakpoint:
    """A single cache breakpoint."""
    label: str          # "tools", "sys", "hist", etc.
    msg_index: int      # index in messages list (-1 = tools)
    chars: int          # cumulative chars up to and including this breakpoint
    est_tokens: int     # estimated tokens (chars // 4)
    eligible: bool      # meets 1024-token minimum


@dataclass
class CacheProbeResult:
    """Output of :func:`estimate_cache_ratio`."""

    breakpoints: list[CacheBreakpoint] = field(default_factory=list)

    total_chars: int = 0
    """Total chars across all messages + tools."""

    cacheable_chars: int = 0
    """Chars covered by eligible (≥ 1 024 tok) breakpoints."""

    ratio_pct: int = 0
    """Expected cache hit ratio as integer percent (0–100)."""

    summary: str = ""
    """Compact human-readable summary string."""

    request_headers: dict[str, Any] = field(default_factory=dict)
    """Snapshot of cache-related pseudo-headers for logging."""


def estimate_cache_ratio(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> CacheProbeResult:
    """Estimate the prompt-cache hit ratio for a single request.

    Parameters
    ----------
    messages:
        The messages list **after** ``_apply_cache_control`` has been called,
        i.e. containing ``cache_control`` markers in content blocks.
    tools:
        The tools list **after** ``_apply_cache_control`` (may have
        ``cache_control`` on the last entry).

    Returns
    -------
    CacheProbeResult
        Contains per-breakpoint details and an overall ``ratio_pct``.
    """
    result = CacheProbeResult()

    # ── 1. Measure total chars ──────────────────────────────────────────────
    total_chars = 0

    # Tools chars
    tools_chars = 0
    if tools:
        tools_str = json.dumps(tools, ensure_ascii=False)
        tools_chars = len(tools_str)
        total_chars += tools_chars

    # Message chars (cumulative, in order)
    cumulative_chars = tools_chars
    msg_char_cumulative: list[int] = []
    for msg in messages:
        c = _content_chars(msg.get("content"))
        cumulative_chars += c
        msg_char_cumulative.append(cumulative_chars)
        total_chars += c

    result.total_chars = total_chars

    # ── 2. Locate breakpoints ───────────────────────────────────────────────
    breakpoints: list[CacheBreakpoint] = []

    # Tools breakpoint (if tools list ends with cache_control)
    if tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict) and "cache_control" in last_tool:
            est = tools_chars // _CHARS_PER_TOKEN
            breakpoints.append(CacheBreakpoint(
                label="tools",
                msg_index=-1,
                chars=tools_chars,
                est_tokens=est,
                eligible=est >= _MIN_CACHE_TOKENS,
            ))

    # Message breakpoints
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if _has_cache_control(content):
            cum = msg_char_cumulative[i]
            est = cum // _CHARS_PER_TOKEN
            role = msg.get("role", "?")
            breakpoints.append(CacheBreakpoint(
                label=_label_for(role, i, messages),
                msg_index=i,
                chars=cum,
                est_tokens=est,
                eligible=est >= _MIN_CACHE_TOKENS,
            ))

    # ── 3. Calculate cacheable chars ────────────────────────────────────────
    # The largest eligible breakpoint covers the most chars that will be cached.
    # Smaller eligible BPs are subsets, so the max wins.
    eligible_bps = [bp for bp in breakpoints if bp.eligible]
    if eligible_bps:
        # Each BP is cumulative; the last eligible one wins if we have a cache
        # prefix.  But in practice providers cache each BP independently as
        # long as the prefix is stable.  We use the largest cumulative chars
        # as the upper-bound estimate.
        cacheable_chars = max(bp.chars for bp in eligible_bps)
    else:
        cacheable_chars = 0

    result.cacheable_chars = cacheable_chars
    result.breakpoints = breakpoints

    if total_chars > 0:
        result.ratio_pct = min(100, int(cacheable_chars / total_chars * 100))
    else:
        result.ratio_pct = 0

    # ── 4. Request-headers snapshot ─────────────────────────────────────────
    result.request_headers = {
        "x-cache-breakpoints": len(breakpoints),
        "x-cache-eligible-breakpoints": len(eligible_bps),
        "x-cache-total-chars": total_chars,
        "x-cache-cacheable-chars": cacheable_chars,
        "x-cache-ratio-pct": result.ratio_pct,
        "x-cache-breakpoint-detail": [
            {
                "label": bp.label,
                "msg_index": bp.msg_index,
                "est_tokens": bp.est_tokens,
                "eligible": bp.eligible,
            }
            for bp in breakpoints
        ],
    }

    # ── 5. Summary string ───────────────────────────────────────────────────
    if breakpoints:
        bp_parts = []
        for bp in breakpoints:
            tok_k = f"{bp.est_tokens // 1000}k" if bp.est_tokens >= 1000 else str(bp.est_tokens)
            flag = "" if bp.eligible else "⚠"
            bp_parts.append(f"{bp.label}={tok_k}tok{flag}")
        result.summary = f"{result.ratio_pct}% ({', '.join(bp_parts)})"
    elif total_chars == 0:
        result.summary = "n/a (empty)"
    else:
        result.summary = "0% (no cache_control markers)"

    return result


def _label_for(role: str, idx: int, messages: list[dict]) -> str:
    """Generate a short human label for a cache breakpoint position."""
    if role == "system":
        # Count how many system messages are before this one
        sys_count = sum(1 for m in messages[:idx] if m.get("role") == "system")
        return f"sys{sys_count}" if sys_count > 0 else "sys"
    if role == "user":
        user_count = sum(1 for m in messages[:idx] if m.get("role") == "user")
        return f"user{user_count}" if user_count > 0 else "user"
    if role == "assistant":
        return "hist"
    return f"{role}[{idx}]"
