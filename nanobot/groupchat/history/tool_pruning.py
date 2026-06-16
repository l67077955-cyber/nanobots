"""Context pruning for tool results.

Prunes old tool-result messages to keep the prompt within token budgets.
When the estimated usage exceeds the soft threshold, replaces old voluminous
tool outputs with a concise 1-line summary (e.g. `[exec] ran 'npm test' -> exit 0, 47 lines`).

Only tool-result messages *before* the last ``keep_recent`` assistant turns
are eligible for pruning. User and assistant messages are never modified.

Tuning note: defaults were raised (soft_ratio ~0.55, keep_recent=4, larger max)
to reduce premature loss of useful tool output while still protecting the window.
See history_settings.context_pruning for user overrides.
"""

from __future__ import annotations

import json
import re
from typing import Any

CHARS_PER_TOKEN = 4  # conservative fallback only

# Prefer real tiktoken-based estimation (handles Chinese, tool_calls, images etc accurately)
# Falls back to char//4 when the helper is unavailable or fails.
try:
    from nanobot.utils.helpers import estimate_message_tokens as _estimate_one_message_tokens
except Exception:
    _estimate_one_message_tokens = None  # type: ignore[assignment]

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_SOFT_RATIO = 0.55    # start pruning when > 55% of window (was 0.3; tuned to reduce info loss)
DEFAULT_KEEP_RECENT = 4      # protect last N assistant turns
DEFAULT_MAX_CHARS = 2_000    # prune tool outputs larger than this (internal hard threshold before summarization)


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


def _estimate_total_tokens(messages: list[dict[str, Any]]) -> int:
    """Best-effort token estimate for the whole message list.

    Uses the project's tiktoken helper when available (much better for CJK,
    tool_calls framing, etc). Falls back to the old char//4 rule.
    """
    if _estimate_one_message_tokens is not None:
        try:
            return sum(int(_estimate_one_message_tokens(m) or 0) for m in messages)
        except Exception:
            pass
    # Fallback (original behavior)
    return sum(_estimate_message_chars(m) for m in messages) // CHARS_PER_TOKEN + len(messages) * 3  # small framing overhead


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
    return 0


def _build_tool_map(messages: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Map tool_call_id to (tool_name, tool_arguments) from assistant messages."""
    result: dict[str, tuple[str, str]] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    tcid = tc.get("id", "")
                    name = fn.get("name", "")
                    args = fn.get("arguments", "")
                    if tcid and name:
                        result[tcid] = (name, args)
    return result


def _summarize_tool_result(tool_name: str, tool_args: str, tool_content: str) -> str:
    """Create an informative 1-line summary of a tool call + result.

    Returns strings like::
        [exec] ran `npm test` -> exit 0, 47 lines | head: 'test_foo.py::test_bar PASSED'
        [read_file] read config.py (1,200 chars)

    When replacing large tool outputs we intentionally keep a short "head"
    preview so the agent still has some signal without the full bloat.
    """
    try:
        args = json.loads(tool_args) if tool_args else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    content = tool_content or ""
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    if tool_name == "exec":
        cmd = args.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        exit_match = re.search(r'(?:exit[_ ]?code|returncode)\s*[:=]\s*(-?\d+)', content, re.IGNORECASE)
        exit_code = exit_match.group(1) if exit_match else "?"
        head = ""
        # Extract a short useful preview from the actual output (skip trailing exit noise)
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if lines:
            # take first 1-2 non-trivial lines, keep very short
            preview_lines = [l[:70] for l in lines[:2] if len(l) > 3][:2]
            if preview_lines:
                head = " | head: " + " ; ".join(preview_lines)[:140]
        return f"[exec] ran `{cmd}` -> exit {exit_code}, {line_count} lines{head}"

    if tool_name == "read_file":
        path = args.get("path", "?")
        head = ""
        if content_len > 300:
            first = content.splitlines()[0][:80] if content.splitlines() else ""
            if first:
                head = f" | head: {first}"
        return f"[read_file] read {path} ({content_len:,} chars){head}"

    if tool_name in ("write_file", "edit_file"):
        path = args.get("path", "?")
        return f"[{tool_name}] modified {path} ({content_len:,} chars result)"

    if tool_name in ("web_search", "smart_search"):
        query = args.get("query", "?")
        head = ""
        if content_len > 400:
            # Try to surface the first title/link or result snippet
            first_line = next((l.strip() for l in content.splitlines() if l.strip()), "")[:90]
            if first_line:
                head = f" | e.g. {first_line}"
        return f"[{tool_name}] query='{query}' ({content_len:,} chars result){head}"

    if tool_name in ("web_fetch", "smart_fetch"):
        url = args.get("url", "?")
        head = ""
        if content_len > 400:
            first = content[:120].replace("\n", " ").strip()
            head = f" | head: {first[:100]}"
        return f"[{tool_name}] fetched {url} ({content_len:,} chars result){head}"

    if tool_name == "list_dir":
        path = args.get("dir", args.get("path", "."))
        return f"[list_dir] scanned {path} ({content_len:,} chars result)"

    if tool_name in ("chatroom_send", "wait"):
        return f"[{tool_name}] ({content_len:,} chars result)"

    # Generic fallback
    first_arg = ""
    for k, v in list(args.items())[:2]:
        sv = str(v)[:40]
        first_arg += f" {k}={sv}"
    return f"[{tool_name}]{first_arg} ({content_len:,} chars result)"


def prune_messages(
    messages: list[dict[str, Any]],
    context_window_tokens: int,
    *,
    soft_ratio: float | None = None,
    keep_recent: int | None = None,
    max_chars: int | None = None,
    ignored_tool_call_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Prune old tool-result messages to fit within the context window.

    Returns a new list (shallow copy) with pruned messages. User/assistant messages
    are never modified. Only tool results before the last ``keep_recent`` assistant
    turns are pruned by substituting large outputs with a single generic summary line.

    ignored_tool_call_ids: skip pruning tool results with these IDs (e.g. just
    forgotten via ForgetTool) so forget and compression stay coordinated.
    """
    try:
        from nanobot.groupchat.history import history_settings as hs
        if soft_ratio is None:
            soft_ratio = hs.pruning_soft_ratio()
        if keep_recent is None:
            keep_recent = hs.pruning_keep_recent()
        if max_chars is None:
            max_chars = hs.pruning_soft_max_chars()
    except Exception:
        pass

    if soft_ratio is None:
        soft_ratio = DEFAULT_SOFT_RATIO
    if keep_recent is None:
        keep_recent = DEFAULT_KEEP_RECENT
    if max_chars is None:
        max_chars = DEFAULT_MAX_CHARS

    if not messages or context_window_tokens <= 0:
        return messages

    total_tokens = _estimate_total_tokens(messages)
    ratio = total_tokens / max(1, context_window_tokens)

    if ratio < soft_ratio:
        return messages

    cutoff = _find_cutoff_index(messages, keep_recent)
    if cutoff <= 0:
        return messages

    prunable: list[int] = []
    for i in range(cutoff):
        if messages[i].get("role") == "tool":
            tcid = messages[i].get("tool_call_id", "")
            if ignored_tool_call_ids and tcid in ignored_tool_call_ids:
                continue
            content = messages[i].get("content", "")
            if isinstance(content, str) and len(content) > max_chars:
                prunable.append(i)

    if not prunable:
        return messages

    result = list(messages)
    tool_map = _build_tool_map(messages)

    for i in prunable:
        content = result[i].get("content", "")
        if not isinstance(content, str) or len(content) <= max_chars:
            continue
            
        tcid = result[i].get("tool_call_id", "")
        if ignored_tool_call_ids and tcid in ignored_tool_call_ids:
            continue
        tool_name, tool_args = tool_map.get(tcid, ("unknown_tool", ""))
        
        summary = _summarize_tool_result(tool_name, tool_args, content)
        result[i] = {**result[i], "content": summary}

    return result


async def prune_conversation_tail_with_summary(
    messages: list[dict[str, Any]],
    sys_msg_count: int,
    keep_turns: int = 3,
    *,
    provider: Any = None,
    model: str = "",
    agent_name: str = "",
    min_dropped_for_summary: int = 5,
) -> int:
    """Prune old conversation turns, summarising dropped messages via LLM first.
    
    Unlike ``prune_conversation_tail`` which silently discards, this function:
    1. Extracts messages that would be dropped
    2. If dropped < ``min_dropped_for_summary``, silently discards (not worth LLM call)
    3. Otherwise calls the LLM to produce a structured summary
    4. Injects the summary as a system message right after ``sys_msg_count``
       (replacing any prior summary at that position to prevent accumulation)
    5. Keeps the last ``keep_turns * 3`` messages after the summary
    
    Returns the number of messages dropped (excluding the injected summary).
    """
    max_conv = keep_turns * 3
    conv_msgs = messages[sys_msg_count:]
    if len(conv_msgs) <= max_conv:
        return 0

    dropped_msgs = conv_msgs[:-max_conv]
    kept_msgs = conv_msgs[-max_conv:]
    dropped_count = len(dropped_msgs)

    # ── Small drops: not worth an LLM call ──
    if dropped_count < min_dropped_for_summary:
        messages[sys_msg_count:] = kept_msgs
        return dropped_count

    # ── Build conversation text for summarisation ──
    lines: list[str] = []
    for m in dropped_msgs:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            content = " ".join(text_parts)
        if isinstance(content, str) and content.strip():
            truncated = content[:2000] + ("..." if len(content) > 2000 else "")
            lines.append(f"[{role}]: {truncated}")

    if not lines:
        messages[sys_msg_count:] = kept_msgs
        return dropped_count

    conversation_text = "\n".join(lines)

    # ── Structured summary prompt ──
    prompt = (
        f"以下是群聊中已超出上下文窗口的早期对话（共 {dropped_count} 条消息）。\n"
        "请用结构化格式摘要，保留关键信息：\n\n"
        "## 目标\n（本轮讨论要解决什么问题）\n\n"
        "## 关键进展\n（已完成的重要发现、决策、结论）\n\n"
        "## 待解决\n（尚未达成共识或需要继续讨论的问题）\n\n"
        "## 关键数据\n（具体的数值、路径、URL、配置等硬事实）\n\n"
        "不超过 300 字。\n\n"
        f"{conversation_text}"
    )

    summary_text = ""
    if provider is not None and model:
        try:
            from loguru import logger as _logger
            response = await provider.chat_with_retry(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                max_tokens=600,
            )
            summary_text = (response.content or "").strip()
            if summary_text:
                _logger.info(
                    "prune_conversation_tail: AI summarised {} dropped msgs → {} chars",
                    dropped_count, len(summary_text),
                )
        except Exception as e:
            from loguru import logger as _logger
            _logger.warning(
                "prune_conversation_tail: AI summary failed ({}), falling back to drop", e
            )

    # ── Rebuild messages, appending summary to last system msg to preserve prefix cache ──
    if summary_text:
        summary_block = (
            f"\n\n[上下文摘要 — 以下 {dropped_count} 条早期消息已被压缩]\n"
            + summary_text
        )
        # Append to the last original system message (sys_msg_count - 1)
        # instead of inserting a new message, to keep message count stable
        # for DeepSeek automatic prefix caching.
        last_sys_idx = sys_msg_count - 1
        if last_sys_idx >= 0 and messages[last_sys_idx].get("role") == "system":
            orig_content = messages[last_sys_idx].get("content", "")
            if isinstance(orig_content, str):
                # Strip previous summary if present (starts after double newline)
                parts = orig_content.split("\n\n[上下文摘要", 1)
                messages[last_sys_idx]["content"] = parts[0] + summary_block
            else:
                messages[last_sys_idx]["content"] = str(orig_content) + summary_block
        messages[sys_msg_count:] = kept_msgs
    else:
        messages[sys_msg_count:] = kept_msgs

    return dropped_count
