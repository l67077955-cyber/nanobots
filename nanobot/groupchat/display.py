"""Centralized display formatting for group chat.

All visual formatting, headers, and status message templates live here.
Design: clean monochrome Unicode, minimal emoji, professional tone.
"""

from __future__ import annotations


# ── Tool Labels ──────────────────────────────────────────────
# Concise verb labels instead of emoji spam.
TOOL_LABELS: dict[str, str] = {
    "web_search": "search",
    "web_fetch": "fetch",
    "exec": "exec",
    "read_file": "read",
    "write_file": "write",
    "edit_file": "edit",
    "list_dir": "ls",
    "chatroom_send": "send",
    "wait": "wait",
    "yield_turn": "yield",
}


def tool_icon(tool_name: str) -> str:
    """Return label for a tool (kept for backward compat with engine.py)."""
    return TOOL_LABELS.get(tool_name, tool_name)


# ── Badges & Headers ─────────────────────────────────────────

def agent_badge(agent_name: str, leader: str | None) -> str:
    """Return ' ★' if agent is leader, else ''."""
    return " ★" if leader == agent_name else ""


def agent_header(
    agent_name: str,
    *,
    leader: str | None = None,
    idx: int = 0,
    total: int = 0,
    mode: str = "group",
) -> str:
    """Build the display header for an agent's turn.

    Examples:
        '▍Benjamin ★ [1/4]\\n\\n'   (group/serial)
        '▍Harper [2/4]: '          (broadcast)
    """
    badge = agent_badge(agent_name, leader)
    round_tag = f" [{idx}/{total}]" if total > 1 else ""
    if mode == "broadcast":
        return f"▍{agent_name}{round_tag}: "
    return f"▍{agent_name}{badge}{round_tag}\n\n"


# ── Status Messages ──────────────────────────────────────────

def thinking_msg(
    agent_name: str,
    model_short: str,
    *,
    leader: str | None = None,
    idx: int = 0,
    total: int = 0,
) -> str:
    """'◌ Harper (grok-4-1) [2/4]'"""
    badge = agent_badge(agent_name, leader)
    tag = f" [{idx}/{total}]" if total > 1 else ""
    return f"◌ {agent_name}{badge} · {model_short}{tag}"


def completion_msg(
    agent_name: str,
    latency: float,
    iterations: int = 1,
    tools_used: list[str] | None = None,
) -> str:
    """'● Harper — 3.2s, tools: search, fetch'"""
    parts = [f"{latency:.1f}s"]
    if iterations > 1:
        parts.append(f"{iterations} iter")
    if tools_used:
        parts.append(f"tools: {', '.join(tools_used)}")
    detail = ", ".join(parts)
    return f"● {agent_name} — {detail}" if detail else ""


def error_msg(agent_name: str, error: str, latency: float = 0) -> str:
    if latency:
        return f"✗ {agent_name} failed ({latency:.1f}s): {error}"
    return f"✗ {agent_name} failed: {error}"


def empty_reply_msg(agent_name: str) -> str:
    return f"✗ {agent_name}: empty reply"


def tool_in_progress_msg(header: str) -> str:
    return f"{header}– working…"


# ── Broadcast-specific ───────────────────────────────────────

def broadcast_start_msg(agents: list[str], timeout: int) -> str:
    total = len(agents)
    names = " · ".join(agents)
    return (
        f"━━ Broadcast — {total} agents ━━\n"
        f"{names}\n"
        f"timeout {timeout}s"
    )


def broadcast_complete_msg(
    completed: int,
    total: int,
    comm_count: int = 0,
) -> str:
    msg = f"━━ {completed}/{total} agents done"
    if comm_count > 0:
        msg += f", {comm_count} messages exchanged"
    msg += " ━━"
    return msg


def thread_bar(used: int, capacity: int) -> str:
    """Render pool status as a visual thread bar.

    Example: '▰▰▰▱▱▱▱▱▱▱▱▱ 3/12'
    """
    filled = "▰" * used
    empty = "▱" * (capacity - used)
    return f"{filled}{empty} {used}/{capacity}"


def chat_chain_summary(
    history: list,
    *,
    max_preview: int = 120,
) -> str:
    """Format a readable chat chain from mailbox history.

    Output:
        📜 对话链:
        1. Harper → All: 我认为应该选纪律委员...
        2. Ares → Harper: 同意你的观点，但需要...
        3. Lucas → All: 我有不同看法...
        4. Benjamin → All: 综合大家意见...
    """
    if not history:
        return ""

    lines = ["📜 对话链:"]
    for i, msg in enumerate(history, 1):
        sender = msg.sender if hasattr(msg, "sender") else str(msg.get("sender", "?"))
        targets = msg.targets if hasattr(msg, "targets") else msg.get("targets", [])
        content = msg.content if hasattr(msg, "content") else str(msg.get("content", ""))

        to = ", ".join(targets) if isinstance(targets, list) else str(targets)
        preview = content.replace("\n", " ")
        if len(preview) > max_preview:
            preview = preview[:max_preview] + "…"

        lines.append(f"  {i}. {sender} → {to}: {preview}")

    return "\n".join(lines)


def synthesis_start_msg(count: int) -> str:
    return f"━━ Synthesis — {count} agent(s) ━━"


def synthesis_agent_msg(
    agent_name: str,
    model_short: str,
    is_leader: bool,
    idx: int,
    total: int,
) -> str:
    label = "synthesizing" if is_leader else "reviewing"
    return f"◌ {agent_name} {label}… ({model_short}) [{idx + 1}/{total}]"


def tool_call_line(agent_name: str, tool_name: str, short_arg: str = "") -> str:
    """Format a tool call for serial/orchestra mode.

    Returns: '  ▸ Harper · search(Trump news)'
    """
    label = TOOL_LABELS.get(tool_name, tool_name)
    return f"  ▸ {agent_name} · {label}({short_arg})"


def tool_result_line(preview: str, result_len: int) -> str:
    """Format a tool result for inline display.

    Returns: '    └ Results for: Trump… (1234c)'
    """
    ellipsis = "…" if result_len > 80 else ""
    return f"    └ {preview}{ellipsis} ({result_len:,}c)"


# ── Tool Activity Display (broadcast mode) ───────────────────

def tool_activity_msg(
    agent_name: str,
    tool_name: str,
    args: dict,
) -> str:
    """Format a tool call for broadcast display.

    Clean one-line format:
        ▸ Harper · search "Trump latest news"
        ▸ Lucas  · fetch  reuters.com/world/…
        ▸ Ben    · exec   python3 analysis.py
    """
    label = TOOL_LABELS.get(tool_name, tool_name)
    if tool_name == "web_search":
        query = args.get("query", "")
        return f"  ▸ {agent_name} · {label} \"{query}\""
    elif tool_name == "web_fetch":
        url = (args.get("url", "") or "")
        if len(url) > 65:
            url = url[:65] + "…"
        return f"  ▸ {agent_name} · {label} {url}"
    elif tool_name == "exec":
        cmd = (args.get("command", "") or "")[:55]
        return f"  ▸ {agent_name} · {label} {cmd}"
    elif tool_name == "read_file":
        path = (args.get("path", "") or "").split("/")[-1]
        return f"  ▸ {agent_name} · {label} {path}"
    elif tool_name in ("write_file", "edit_file"):
        path = (args.get("path", "") or "").split("/")[-1]
        return f"  ▸ {agent_name} · {label} {path}"
    elif tool_name == "list_dir":
        path = args.get("path", "") or "."
        return f"  ▸ {agent_name} · {label} {path}"
    else:
        short = ""
        if args:
            first = list(args.values())[0]
            if isinstance(first, str):
                short = first[:40]
        suffix = f" {short}" if short else ""
        return f"  ▸ {agent_name} · {label}{suffix}"


def tool_result_brief(
    agent_name: str,
    tool_name: str,
    result: str,
) -> str:
    """Format a tool result summary for broadcast display.

        └ 5 results
        └ fetched (12,345c)
    """
    rlen = len(result) if result else 0
    if tool_name == "web_search":
        import re
        m = re.search(r'\((\d+) results?\)', result[:100]) if result else None
        count = int(m.group(1)) if m else max(result.count("\n") // 3, 1) if result else 0
        return f"    └ {count} results"
    elif tool_name == "web_fetch":
        return f"    └ fetched ({rlen:,}c)"
    elif tool_name == "exec":
        preview = (result or "").strip().replace("\n", " ")[:55]
        return f"    └ {preview}{'…' if rlen > 55 else ''}"
    else:
        return f"    └ ({rlen:,}c)"


# ── Chatroom Communication Display ───────────────────────────

def chatroom_send_msg(
    sender: str,
    to: str,
    message: str,
    *,
    max_len: int = 500,
) -> str:
    """Format an inter-agent chatroom_send.

    Visual design:
        ─── Harper → Lucas ───
        message content here…
    """
    if len(message) > max_len:
        message = message[:max_len] + "…"
    header = f"{sender} → {to}"
    # Pad dashes to frame the header
    pad = max(2, (28 - len(header)) // 2)
    rule = "─" * pad
    return (
        f"{rule} {header} {rule}\n"
        f"{message}"
    )


def chatroom_wait_msg(agent: str, result: str) -> str:
    """Format a wait tool result — short receipt notice only.

    The full message content was already shown when chatroom_send fired,
    so we only indicate who received from whom.

        ← Ares received from Harper
    """
    # Extract sender name from "[sender → target]: ..." format
    import re
    m = re.match(r'\[(\w+)', result)
    sender = m.group(1) if m else "teammate"
    return f"  ← {agent} received from {sender}"


def user_interjection_msg(message: str, *, max_len: int = 500) -> str:
    """Format a user interjection during broadcast.

    Uses double-line border to stand out from agent messages:
        ╔══ USER ══════════════════╗
        message content here…
        ╚═════════════════════════╝
    """
    if len(message) > max_len:
        message = message[:max_len] + "…"
    width = max(26, len("USER") + 8)
    top_pad = width - len(" USER ") - 2
    return (
        f"╔══ USER {'═' * top_pad}╗\n"
        f"{message}\n"
        f"╚{'═' * width}╝"
    )


def yield_turn_msg(from_agent: str, to_agent: str, reason: str = "") -> str:
    """Format a yield-turn event.

        ↻ Harper yield → Lucas (原因: ...)
    """
    suffix = f" ({reason})" if reason else ""
    return f"  ↻ {from_agent} yield → {to_agent}{suffix}"


def speak_order_msg(order: list[str], leader: str | None = None) -> str:
    """Format the current speaking order.

        🗣 发言顺序: Ares → Lucas → Harper → Benjamin 👑
    """
    parts = []
    for name in order:
        badge = " 👑" if name == leader else ""
        parts.append(f"{name}{badge}")
    return f"🗣 发言顺序: {' → '.join(parts)}"
