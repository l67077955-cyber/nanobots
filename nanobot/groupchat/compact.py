"""Context compaction for group chat — inspired by Claude Code's three-tier system.

Provides:
- ``TokenMonitor``:    Track token usage and trigger compaction thresholds
- ``MicroCompactor``:  Clear old tool results to reclaim context space
- ``AutoCompactor``:   Summarize old conversation history via LLM
- ``compact_history``: One-call helper for engine integration

The design follows Claude Code's philosophy of *proactive* context management:
instead of waiting for token overflow, continuously monitor and respond in tiers.

Tier 1 — Micro Compact (every round):
    Replace old tool result contents with ``[已清理]`` placeholder.
    Cheap, no LLM call, preserves conversation structure.

Tier 2 — Auto Compact (threshold-triggered):
    Summarize old conversation segments via a fast LLM call.
    Triggered when estimated tokens exceed a configurable ratio.

Tier 3 — Hard Truncation (emergency fallback):
    Drop oldest messages when all else fails.
    Existing ``max_messages`` behavior, now as last resort.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


# ── Constants ─────────────────────────────────────────────────

# Token estimation: ~3.5 chars per token for mixed CJK/English
_CHARS_PER_TOKEN = 3.5
# Conservative padding multiplier (same as Claude Code's 4/3)
_ESTIMATION_PAD = 4 / 3

# Placeholder for cleared tool results
CLEARED_PLACEHOLDER = "[工具结果已清理]"

# Tools whose results can be safely cleared (matches Claude Code's set)
COMPACTABLE_TOOLS = frozenset({
    "web_search", "web_fetch", "exec",
    "read_file", "list_dir",
    # write_file/edit_file results are typically short confirmations — keep them
})


# ── Token Monitor ─────────────────────────────────────────────

@dataclass
class CompactThresholds:
    """Configurable thresholds as ratios of context window."""
    warning: float = 0.55     # 55% — log warning
    micro_compact: float = 0.65  # 65% — trigger micro compact
    auto_compact: float = 0.80   # 80% — trigger auto compact (LLM summary)
    blocking: float = 0.92    # 92% — force hard truncation


class TokenMonitor:
    """Track estimated token usage and determine compaction level.

    Usage::

        monitor = TokenMonitor(context_window=128_000)
        level = monitor.check(messages)
        # level ∈ {None, "warning", "micro", "auto", "blocking"}
    """

    # Circuit breaker: stop auto-compact after N consecutive failures
    CIRCUIT_BREAKER_MAX = 3

    def __init__(
        self,
        context_window: int = 128_000,
        thresholds: CompactThresholds | None = None,
    ):
        self.context_window = context_window
        self.thresholds = thresholds or CompactThresholds()
        self._compact_failures = 0
        self._last_compact_tokens = 0
        self._last_check_time = 0.0

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimation for a message list.

        Uses char-count heuristic with conservative padding.
        Not perfectly accurate, but fast and sufficient for threshold checks.
        """
        total_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # Structured content blocks
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(block.get("text", ""))
            # Count role/name overhead
            total_chars += len(m.get("role", "")) + len(m.get("name", ""))

        return int(total_chars / _CHARS_PER_TOKEN * _ESTIMATION_PAD)

    def check(self, messages: list[dict[str, Any]]) -> str | None:
        """Check token usage and return compaction level.

        Returns:
            None — within safe range
            "warning" — approaching limit, log only
            "micro" — trigger micro compact
            "auto" — trigger auto compact (LLM summary)
            "blocking" — emergency, force hard truncation
        """
        tokens = self.estimate_tokens(messages)
        ratio = tokens / self.context_window if self.context_window > 0 else 0
        self._last_check_time = _time.time()

        if ratio >= self.thresholds.blocking:
            return "blocking"
        elif ratio >= self.thresholds.auto_compact:
            # Circuit breaker check
            if self._compact_failures >= self.CIRCUIT_BREAKER_MAX:
                logger.warning(
                    "TokenMonitor: auto-compact circuit breaker open "
                    "({} consecutive failures)",
                    self._compact_failures,
                )
                return "blocking"  # Fall through to hard truncation
            return "auto"
        elif ratio >= self.thresholds.micro_compact:
            return "micro"
        elif ratio >= self.thresholds.warning:
            logger.info(
                "TokenMonitor: context at {:.0%} ({} tokens / {} window)",
                ratio, tokens, self.context_window,
            )
            return "warning"
        return None

    def record_compact_success(self, post_tokens: int) -> None:
        """Record successful compaction."""
        self._compact_failures = 0
        self._last_compact_tokens = post_tokens

    def record_compact_failure(self) -> None:
        """Record failed compaction attempt."""
        self._compact_failures += 1
        logger.warning(
            "TokenMonitor: compact failure #{}/{}",
            self._compact_failures, self.CIRCUIT_BREAKER_MAX,
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "context_window": self.context_window,
            "compact_failures": self._compact_failures,
            "last_compact_tokens": self._last_compact_tokens,
        }


# ── Micro Compactor ───────────────────────────────────────────

class MicroCompactor:
    """Clear old tool result contents to reclaim context space.

    Preserves conversation structure (role/name/tool_call_id) but replaces
    the content of old tool results with a compact placeholder.

    Only clears results from tools in ``COMPACTABLE_TOOLS``.
    Always keeps the most recent ``keep_recent`` tool results intact.

    Inspired by Claude Code's ``microCompact.ts``.
    """

    def __init__(self, keep_recent: int = 3):
        self.keep_recent = max(1, keep_recent)  # Always keep at least 1

    def compact(
        self,
        history: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], int]:
        """Apply micro compaction to chat history.

        Args:
            history: List of ``{"sender": ..., "content": ...}`` dicts.

        Returns:
            (compacted_history, tokens_saved_estimate)
        """
        # Find tool result messages (from agents, containing tool indicators)
        tool_indices = self._find_tool_results(history)

        if len(tool_indices) <= self.keep_recent:
            return history, 0  # Nothing to compact

        # Keep the most recent N tool results, clear the rest
        to_clear = set(tool_indices[:-self.keep_recent])

        tokens_saved = 0
        result = []
        for i, m in enumerate(history):
            if i in to_clear:
                old_content = m.get("content", "")
                old_len = len(old_content)
                new_content = CLEARED_PLACEHOLDER
                tokens_saved += int(
                    (old_len - len(new_content)) / _CHARS_PER_TOKEN
                )
                result.append({**m, "content": new_content})
            else:
                result.append(m)

        if tokens_saved > 0:
            logger.info(
                "MicroCompact: cleared {} tool results, ~{} tokens saved "
                "(kept last {})",
                len(to_clear), tokens_saved, self.keep_recent,
            )

        return result, tokens_saved

    def compact_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Apply micro compaction to API-format messages.

        Targets messages with role='tool' or role='user' containing tool results.
        """
        # Identify tool result messages by role
        tool_indices = []
        for i, m in enumerate(messages):
            role = m.get("role", "")
            name = m.get("name", "")
            content = m.get("content", "")

            # Tool result messages from OpenAI-compatible APIs
            if role == "tool":
                tool_name = name or m.get("tool_name", "")
                if not tool_name or tool_name in COMPACTABLE_TOOLS:
                    tool_indices.append(i)
            # Also check for inline tool results in assistant messages
            # that contain structured tool call data
            elif role == "function":
                if name in COMPACTABLE_TOOLS:
                    tool_indices.append(i)

        if len(tool_indices) <= self.keep_recent:
            return messages, 0

        to_clear = set(tool_indices[:-self.keep_recent])
        tokens_saved = 0
        result = []
        for i, m in enumerate(messages):
            if i in to_clear:
                content = m.get("content", "")
                content_len = len(content) if isinstance(content, str) else 0
                tokens_saved += int(
                    (content_len - len(CLEARED_PLACEHOLDER)) / _CHARS_PER_TOKEN
                )
                result.append({**m, "content": CLEARED_PLACEHOLDER})
            else:
                result.append(m)

        if tokens_saved > 0:
            logger.info(
                "MicroCompact (messages): cleared {} tool results, ~{} tokens saved",
                len(to_clear), tokens_saved,
            )

        return result, tokens_saved

    @staticmethod
    def _find_tool_results(history: list[dict[str, str]]) -> list[int]:
        """Find indices of messages that look like tool results.

        Heuristic: messages from system-like senders or containing
        tool output indicators (⚡, 🔧, exec output patterns, etc.)
        """
        indicators = [
            "⚡", "🔧", "🔍",  # Tool display indicators
            "↳",               # Tool result prefix
            "tool_call",       # Internal marker
        ]
        tool_indices = []
        for i, m in enumerate(history):
            sender = m.get("sender", "")
            content = m.get("content", "")

            # Skip user messages and empty content
            if sender == "用户" or sender == "系统" or not content:
                continue

            # Already cleared — skip to avoid double-counting
            if content == CLEARED_PLACEHOLDER:
                continue

            # Check if content looks like a tool result
            # (very long content from agents is likely tool output)
            is_tool_like = (
                len(content) > 2000  # Long responses are likely tool dumps
                or any(ind in content[:100] for ind in indicators)
            )
            if is_tool_like:
                tool_indices.append(i)

        return tool_indices


# ── Auto Compactor ────────────────────────────────────────────

class AutoCompactor:
    """Summarize old conversation history using an LLM.

    When token usage exceeds the auto-compact threshold, this compactor:
    1. Takes the oldest N messages (before a boundary)
    2. Sends them to a fast/cheap LLM for summarization
    3. Replaces those messages with a single summary message
    4. Preserves the most recent messages intact

    Inspired by Claude Code's ``compactConversation()`` in compact.ts.
    """

    # Default: summarize all but the last 8 messages
    KEEP_RECENT_MESSAGES = 8

    # Summary generation prompt (Chinese, matching Nanobot's language)
    SUMMARY_PROMPT = (
        "请将以下群聊对话精炼为一段结构化摘要。保留：\n"
        "1. 所有关键决策和结论\n"
        "2. 每个参与者的主要贡献（附 Agent 名字）\n"
        "3. 未解决的问题或分歧\n"
        "4. 工具调用的关键结果（URL、文件路径、数据）\n\n"
        "删除：闲聊、重复确认、空洞的过渡语。\n"
        "输出格式：纯文本，200-500字内。\n\n"
        "---\n\n"
    )

    def __init__(
        self,
        provider: Any = None,
        model: str = "openai/gpt-4.1-nano",
        keep_recent: int | None = None,
    ):
        self._provider = provider
        self._model = model
        self._keep_recent = keep_recent or self.KEEP_RECENT_MESSAGES

    async def compact(
        self,
        history: list[dict[str, str]],
        provider: Any | None = None,
    ) -> tuple[list[dict[str, str]], dict[str, Any]]:
        """Summarize old messages and return compacted history.

        Args:
            history: Full chat history.
            provider: LLM provider to use (overrides init).

        Returns:
            (compacted_history, stats)
        """
        prov = provider or self._provider
        if not prov:
            logger.warning("AutoCompactor: no provider, skipping")
            return history, {"skipped": True, "reason": "no_provider"}

        if len(history) <= self._keep_recent:
            return history, {"skipped": True, "reason": "too_few_messages"}

        # Split: old messages to summarize, recent to keep
        boundary = len(history) - self._keep_recent
        to_summarize = history[:boundary]
        to_keep = history[boundary:]

        # Build summarization input
        conversation_text = "\n\n".join(
            f"[{m['sender']}]: {m['content']}"
            for m in to_summarize
            if m.get("content") and m["content"] != CLEARED_PLACEHOLDER
        )

        if not conversation_text.strip():
            return history, {"skipped": True, "reason": "empty_content"}

        prompt = self.SUMMARY_PROMPT + conversation_text

        t0 = _time.time()
        try:
            # Use the provider's simple chat interface
            summary = await prov.chat(
                messages=[{"role": "user", "content": prompt}],
                model=self._model,
                max_tokens=800,
            )
            latency = _time.time() - t0

            if not summary or not summary.strip():
                return history, {"skipped": True, "reason": "empty_summary"}

            # Build compacted history:
            # [summary_message] + to_keep
            summary_message = {
                "sender": "系统",
                "content": f"[对话摘要 — 自动生成]\n\n{summary.strip()}",
            }

            compacted = [summary_message] + to_keep

            stats = {
                "skipped": False,
                "messages_summarized": len(to_summarize),
                "messages_kept": len(to_keep),
                "summary_chars": len(summary),
                "latency": round(latency, 2),
                "model": self._model,
            }

            logger.info(
                "AutoCompact: summarized {} messages → {} chars in {:.2f}s "
                "(kept {} recent)",
                len(to_summarize), len(summary), latency, len(to_keep),
            )

            return compacted, stats

        except Exception as e:
            logger.error("AutoCompact failed: {}", e)
            return history, {"skipped": True, "reason": str(e)}


# ── One-call integration helper ───────────────────────────────

async def compact_history(
    history: list[dict[str, str]],
    monitor: TokenMonitor,
    micro: MicroCompactor,
    auto: AutoCompactor | None = None,
    max_messages: int = 50,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Full compaction pipeline — call once per round.

    Applies compaction tiers in order:
    1. Micro compact (clear old tool results)
    2. Auto compact (LLM summary) if still over threshold
    3. Hard truncation (emergency) if still over threshold

    Args:
        history: Current chat history.
        monitor: Token monitor instance.
        micro: Micro compactor instance.
        auto: Optional auto compactor (needs LLM provider).
        max_messages: Hard limit for emergency truncation.

    Returns:
        (compacted_history, compaction_stats)
    """
    # Build API-like messages for token estimation
    est_messages = [{"role": "user", "content": m.get("content", "")} for m in history]
    stats: dict[str, Any] = {"level": None, "tiers_applied": []}

    # Check current level
    level = monitor.check(est_messages)
    stats["level"] = level

    if level is None or level == "warning":
        return history, stats

    result = list(history)

    # Tier 1: Micro Compact (always try first)
    if level in ("micro", "auto", "blocking"):
        result, tokens_saved = micro.compact(result)
        stats["tiers_applied"].append("micro")
        stats["micro_tokens_saved"] = tokens_saved

        # Re-check after micro compact
        est_messages = [{"role": "user", "content": m.get("content", "")} for m in result]
        level = monitor.check(est_messages)

        if level is None or level == "warning":
            monitor.record_compact_success(monitor.estimate_tokens(est_messages))
            return result, stats

    # Tier 2: Auto Compact (LLM summary)
    if level in ("auto", "blocking") and auto is not None:
        result, auto_stats = await auto.compact(result)
        stats["tiers_applied"].append("auto")
        stats["auto"] = auto_stats

        if not auto_stats.get("skipped"):
            monitor.record_compact_success(
                monitor.estimate_tokens(
                    [{"role": "user", "content": m.get("content", "")} for m in result]
                )
            )
            return result, stats
        else:
            monitor.record_compact_failure()

    # Tier 3: Hard Truncation (emergency fallback)
    if len(result) > max_messages:
        old_len = len(result)
        result = result[-max_messages:]
        stats["tiers_applied"].append("truncate")
        stats["truncated_messages"] = old_len - len(result)
        logger.warning(
            "Hard truncation: {} → {} messages",
            old_len, len(result),
        )

    return result, stats
