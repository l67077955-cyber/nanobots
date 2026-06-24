"""History → LLM messages conversion with smart truncation.

Handles both groupchat (sender) and LLM (role) input formats.
Independent of prompt construction."""

from __future__ import annotations

import re
from typing import Any, Callable

from nanobot.groupchat.history.context_validator import validate_context

# ── Tool Log Aging ──────────────────────────────────────────────────────────

_TOOL_LOG_RE = re.compile(
    r"(• \w+\([^)]*\) → )"
    r"([^\n]*?)"       # preview text bounded to single line (non-greedy, no cross-line)
    r"(\(\d[\d,]*字\))",  # trailing char count
)

_TOOL_LOG_BLOCK_RE = re.compile(
    r"\n*(?:\[工具调用记录\]|<previous_tool_calls>[\s\S]*?</previous_tool_calls>).*$",
    re.DOTALL,
)

_TOOL_LINE_RE = re.compile(r"^• (\w+)\(", re.MULTILINE)

# Lowest retention: group-chat / coordination tools.
CHATROOM_TOOL_NAMES = frozenset({
    "chatroom_send",
    "wait",
    "quote_message",
    "list_messages",
    "manage_agent",
    "end_discussion",
    "transfer_credits",
    "clear_context",
})

_TEAMMATE_PREFIX_RE = re.compile(r"^\[([^\]]+)\]: ")

_COMPRESS_HEADER = "[早期对话压缩"
_LINE_CAP = 280


def has_tool_log(content: str) -> bool:
    return "[工具调用记录]" in content or "<previous_tool_calls>" in content


def split_text_and_tool_log(content: str) -> tuple[str, str]:
    if not has_tool_log(content):
        return content, ""
    m = _TOOL_LOG_BLOCK_RE.search(content)
    if not m:
        return content, ""
    return content[: m.start()].rstrip(), content[m.start() :]


def age_tool_log(content: str) -> str:
    """Compress tool call previews to first-line summary."""
    if not has_tool_log(content):
        return content

    def _replace(m: re.Match) -> str:
        preview = m.group(2).strip()
        first_line = preview.split("\n")[0][:100]
        if first_line:
            return f"{m.group(1)}{first_line} {m.group(3)}"
        return m.group(1) + m.group(3)

    return _TOOL_LOG_RE.sub(_replace, content)


def strip_tool_log(content: str) -> str:
    """Remove the entire tool-call block, keeping agent body text."""
    return _TOOL_LOG_BLOCK_RE.sub("", content).rstrip()


def _tool_block_inner(content: str) -> str:
    if "<previous_tool_calls>" in content:
        start = content.index("<previous_tool_calls>")
        end = content.find("</previous_tool_calls>", start)
        if end == -1:
            return content[start:]
        return content[start : end + len("</previous_tool_calls>")]
    if "[工具调用记录]" in content:
        start = content.index("[工具调用记录]")
        return content[start:]
    return ""


def has_chatroom_tool_lines(content: str) -> bool:
    inner = _tool_block_inner(content)
    if not inner:
        return False
    return any(m.group(1) in CHATROOM_TOOL_NAMES for m in _TOOL_LINE_RE.finditer(inner))


def strip_chatroom_tool_lines(content: str) -> str:
    """Drop coordination-tool lines; keep substantive tool lines + body text."""
    if not has_tool_log(content):
        return content
    text, tool_block = split_text_and_tool_log(content)
    if not tool_block:
        return content

    kept_lines: list[str] = []
    for line in tool_block.splitlines():
        m = _TOOL_LINE_RE.match(line)
        if m and m.group(1) in CHATROOM_TOOL_NAMES:
            continue
        kept_lines.append(line)

    if "<previous_tool_calls>" in tool_block:
        body = [
            ln for ln in kept_lines
            if ln.strip() and not ln.startswith("<") and not ln.startswith("</")
        ]
        if not body:
            return text.rstrip()
        rebuilt = "\n".join(["<previous_tool_calls>", *body, "</previous_tool_calls>"])
        return (text + "\n\n" + rebuilt).strip() if text else rebuilt

    body = [ln for ln in kept_lines if ln.strip() and ln != "[工具调用记录]"]
    if not body:
        return text.rstrip()
    return (text + "\n\n[工具调用记录]\n" + "\n".join(body)).strip()


def degrade_content(content: str, level: int) -> str:
    """Tiered in-message degradation.

    0 = full
    1 = strip chatroom/coordination tool lines
    2 = compress substantive tool previews
    3 = strip all tool blocks (agent text only)
    """
    if level <= 0:
        return content
    if level == 1:
        return strip_chatroom_tool_lines(content)
    if level == 2:
        return age_tool_log(strip_chatroom_tool_lines(content))
    return strip_tool_log(content)


def _message_char_len(msg: dict[str, Any]) -> int:
    content = msg.get("content", "")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(block.get("text", "")) for block in content if isinstance(block, dict))
    return 0


def _is_human_user_sender(sender: str) -> bool:
    return sender in ("用户", "User", "user")


def _is_human_user_llm(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "user":
        return False
    content = msg.get("content", "")
    if not isinstance(content, str):
        return True
    m = _TEAMMATE_PREFIX_RE.match(content)
    if not m:
        return True
    return _is_human_user_sender(m.group(1))


def _message_label(msg: dict[str, Any]) -> str:
    if "sender" in msg:
        return str(msg.get("sender") or "?")
    role = msg.get("role", "?")
    content = msg.get("content", "")
    if role == "user" and isinstance(content, str):
        m = _TEAMMATE_PREFIX_RE.match(content)
        if m:
            return m.group(1)
    return str(role)


def _compress_sources_text(sources: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in sources:
        raw = msg.get("content", "")
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = degrade_content(raw, 3).strip()
        if not text:
            continue
        if len(text) > _LINE_CAP:
            text = text[: _LINE_CAP - 1] + "…"
        lines.append(f"[{_message_label(msg)}] {text}")
    return "\n".join(lines)


def build_compress_message(
    sources: list[dict[str, Any]],
    max_chars: int,
    *,
    sender_format: bool = False,
) -> dict[str, Any] | None:
    """Merge dropped/overflow messages into one compressed summary block."""
    if not sources or max_chars <= 0:
        return None
    body = _compress_sources_text(sources)
    if not body:
        return None
    header = f"{_COMPRESS_HEADER}（{len(sources)} 条）]\n"
    available = max_chars - len(header)
    if available <= 0:
        return None
    if len(body) > available:
        body = body[: available - 1] + "…"
    content = header + body
    if sender_format:
        return {"sender": "系统", "content": content}
    return {"role": "system", "content": content}


def _merge_chronological_with_compress(
    messages: list[dict[str, Any]],
    mandatory: set[int],
    optional_indices: set[int],
    included: dict[int, tuple[int, str]],
    compress_msg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild list in chronological order; compress block replaces first omitted slot."""
    out: list[dict[str, Any]] = []
    compress_placed = False
    for i, msg in enumerate(messages):
        if i in mandatory:
            out.append(msg)
            continue
        if i not in optional_indices:
            continue
        slot = included.get(i)
        if slot is not None:
            level, content = slot
            if level == 0:
                out.append(msg)
            else:
                out.append({**msg, "content": content})
        elif not compress_placed:
            out.append(compress_msg)
            compress_placed = True
    if not compress_placed:
        out.append(compress_msg)
    return out


def _replace_optional_with_compress(
    messages: list[dict[str, Any]],
    mandatory: set[int],
    optional_indices: list[int],
    compress_msg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep mandatory only; replace all optional messages with one compress block."""
    out: list[dict[str, Any]] = []
    compress_placed = False
    for i, msg in enumerate(messages):
        if i in mandatory:
            out.append(msg)
            continue
        if i not in optional_indices:
            continue
        if not compress_placed:
            out.append(compress_msg)
            compress_placed = True
    if not compress_placed:
        out.append(compress_msg)
    return out


def latest_user_question(history: list[dict]) -> str:
    """Return the latest non-summary user message (for volatile prompt tail)."""
    for msg in reversed(history):
        if msg.get("sender") in ("User", "user", "用户"):
            content = msg.get("content", "")
            if content.startswith("["):
                continue
            return content[:300]
    return ""


def fit_messages_to_tier_budget(
    messages: list[dict[str, Any]],
    max_chars: int,
    *,
    is_mandatory: Callable[[dict[str, Any], int], bool],
    length_fn: Callable[[dict[str, Any]], int] | None = None,
    sender_format: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Fit messages into *max_chars* with tiered retention.

    Escalation ladder:
      1. Keep human user messages at full fidelity
      2. Include agent messages from newest backward
      3. Degrade in-message: chatroom tools → age tools → strip tools
      4. Drop optional messages that still do not fit
      5. Compress dropped/overflow into one summary block; if still over
         budget, compress **all** optional messages into that single block
    """
    if max_chars <= 0 or not messages:
        return list(messages), 0

    measure = length_fn or _message_char_len

    mandatory = {i for i, m in enumerate(messages) if is_mandatory(m, i)}
    mandatory_chars = sum(measure(messages[i]) for i in mandatory)
    budget = max(0, max_chars - mandatory_chars)

    optional_indices = [i for i in range(len(messages)) if i not in mandatory]
    optional_set = set(optional_indices)
    included: dict[int, tuple[int, str]] = {}

    # Pass 1 — newest optional first; degrade before skipping.
    for i in reversed(optional_indices):
        raw = messages[i].get("content", "")
        if not isinstance(raw, str) or not raw.strip():
            continue
        for level in range(4):
            degraded = degrade_content(raw, level)
            cost = len(degraded)
            if cost <= 0:
                continue
            if budget >= cost:
                included[i] = (level, degraded)
                budget -= cost
                break

    omitted_indices = [i for i in optional_indices if i not in included]

    def _build_partial_result(active_included: dict[int, tuple[int, str]]) -> list[dict[str, Any]]:
        partial: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if i in mandatory:
                partial.append(msg)
                continue
            slot = active_included.get(i)
            if slot is None:
                continue
            level, content = slot
            if level == 0:
                partial.append(msg)
            else:
                partial.append({**msg, "content": content})
        return partial

    result = _build_partial_result(included)
    total = sum(measure(m) for m in result)
    needs_compress = bool(omitted_indices) or total > max_chars

    if needs_compress and optional_indices:
        # Pass 2 — summarize omitted messages; shrink included oldest if needed.
        working_included = dict(included)
        omitted_set = list(omitted_indices)
        compress_msg = None
        for _ in range(len(working_included) + 1):
            partial = _build_partial_result(working_included)
            compress_budget = max(0, max_chars - sum(measure(m) for m in partial))
            compress_msg = build_compress_message(
                [messages[i] for i in omitted_set],
                compress_budget,
                sender_format=sender_format,
            )
            if compress_msg or not working_included:
                break
            oldest = min(working_included)
            omitted_set.append(oldest)
            del working_included[oldest]

        if compress_msg:
            included = working_included
            result = _merge_chronological_with_compress(
                messages, mandatory, optional_set, included, compress_msg,
            )
            total = sum(measure(m) for m in result)

        # Pass 3 — still over budget: all optional → one block.
        if total > max_chars:
            compress_budget = max(0, max_chars - mandatory_chars)
            compress_msg = build_compress_message(
                [messages[i] for i in optional_indices],
                compress_budget,
                sender_format=sender_format,
            )
            if compress_msg:
                result = _replace_optional_with_compress(
                    messages, mandatory, optional_indices, compress_msg,
                )
                total = sum(measure(m) for m in result)

        # Pass 4 — truncate compress body if mandatory alone almost fills budget.
        if total > max_chars:
            for m in result:
                if m.get("content", "").startswith(_COMPRESS_HEADER):
                    overhead = total - max_chars
                    content = m.get("content", "")
                    if isinstance(content, str) and len(content) > overhead:
                        m["content"] = content[: len(content) - overhead]
                    break

    skipped = len(optional_indices) - len(included)
    if needs_compress and optional_indices:
        skipped = len(optional_indices)
    return result, skipped


def trim_sender_history(
    history: list[dict[str, str]],
    max_chars: int,
    *,
    protected_indices: set[int] | None = None,
    length_fn: Callable[[dict[str, Any]], int] | None = None,
) -> list[dict[str, str]]:
    """Tiered trim for persisted groupchat history (sender format)."""
    protected = protected_indices or set()

    def _mandatory(msg: dict[str, Any], index: int) -> bool:
        if index in protected:
            return True
        return _is_human_user_sender(msg.get("sender", ""))

    trimmed, _ = fit_messages_to_tier_budget(
        history,
        max_chars,
        is_mandatory=_mandatory,
        length_fn=length_fn,
        sender_format=True,
    )
    return trimmed


def trim_llm_messages(
    messages: list[dict[str, Any]],
    max_chars: int,
    *,
    protect_index_zero: bool = True,
    length_fn: Callable[[dict[str, Any]], int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Tiered trim for LLM-role messages from history_to_messages."""

    def _mandatory(msg: dict[str, Any], index: int) -> bool:
        if protect_index_zero and index == 0:
            return True
        return _is_human_user_llm(msg)

    return fit_messages_to_tier_budget(
        messages,
        max_chars,
        is_mandatory=_mandatory,
        length_fn=length_fn,
        sender_format=False,
    )


def history_to_messages(
    history: list[dict],
    current_agent: str = "",
    max_chars: int = 0,
    pin_first_user: bool = True,          # backward compat, unused
    relevant_agents: list[str] | None = None,
    agent_ranks: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Convert history dicts into LLM API messages."""
    from nanobot.groupchat.display.visibility import can_see_tool_call

    my_rank = (agent_ranks or {}).get(current_agent, 0)

    def _to_msg(m: dict[str, str]) -> dict[str, Any]:
        sender, content = m["sender"], m["content"]
        has_tool = has_tool_log(content)
        if agent_ranks and sender not in ("用户", "系统", current_agent):
            sender_rank = agent_ranks.get(sender, 0)
            if not can_see_tool_call(sender_rank, my_rank) and has_tool:
                content = strip_tool_log(content)
        if sender == "用户":
            return {"role": "user", "content": content}
        if sender == "系统":
            return {"role": "system", "content": content}
        if sender == current_agent:
            return {
                "role": "assistant",
                "content": content,
                "name": sender.replace(" ", "_"),
            }
        return {
            "role": "user",
            "content": f"[{sender}]: {content}",
            "name": sender.replace(" ", "_"),
        }

    if not history:
        return []

    first = history[0]
    is_groupchat = "sender" in first

    allowed = {"用户", "系统"} | set(relevant_agents or [])
    msgs_full = history if not is_groupchat else [
        _to_msg(m) for m in history
        if relevant_agents is None or m["sender"] in allowed
    ]

    if not max_chars or not msgs_full:
        return _merge_consecutive_assistant(msgs_full)

    result, skipped = trim_llm_messages(msgs_full, max_chars)
    result = _merge_consecutive_assistant(result)
    validate_context(result, current_agent, skipped)
    return result


def _merge_consecutive_assistant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge back-to-back assistant messages into one (LLM APIs reject consecutive same-role)."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if out and msg.get("role") == "assistant" and out[-1].get("role") == "assistant":
            prev = out[-1]
            prev_text = prev.get("content") or ""
            cur_text = msg.get("content") or ""
            prev["content"] = f"{prev_text}\n\n{cur_text}".strip()
        else:
            out.append(msg)
    return out