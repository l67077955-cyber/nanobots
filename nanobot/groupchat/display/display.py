"""Centralized display formatting for group chat.

# Verified: Harper has write access to source.

All visual formatting, headers, and status message templates live here.
Design: clean Unicode, role-aware badges, compact and readable.
"""

from __future__ import annotations


def format_token_stats(
    prompt: int,
    completion: int,
    elapsed: float | None = None,
    cost: float | None = None,
    cache_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> str:
    """Format token usage into a compact, human-readable status line.

    Returns a backtick-wrapped string for Telegram monospace rendering.
    Example: `提示:46,902 回复:68 · 9.9s $0.034 💾缓存:23,305`
    With reasoning: `提示:21,811 回复:36+💭56 · 9.8s $0.0265 💾缓存:42`
    """
    if reasoning_tokens > 0:
        visible = completion - reasoning_tokens
        completion_str = f"{visible:,}+💭{reasoning_tokens:,}"
    else:
        completion_str = f"{completion:,}"
    parts = [f"提示:{prompt:,} 回复:{completion_str}"]
    if elapsed is not None:
        parts.append(f"· {elapsed:.1f}s")
    if cost:
        parts.append(f"${cost:.4f}")
    if cache_tokens:
        parts.append(f"💾缓存:{cache_tokens:,}")
    return "`" + " ".join(parts) + "`"


def _shorten_path(path: str, max_parts: int = 2) -> str:
    """Shorten a filesystem path to the last N components.

    '/root/.nanobot/workspace/memory/MEMORY.md' → 'memory/MEMORY.md'
    Short paths are returned as-is.
    """
    if not path or "/" not in path:
        return path
    parts = path.rstrip("/").split("/")
    if len(parts) <= max_parts:
        return path
    return "/".join(parts[-max_parts:])


# ── Tool Labels ──────────────────────────────────────────────
TOOL_LABELS: dict[str, str] = {
    "web_search": "🔍",
    "web_fetch": "🌐",
    "exec": "⚡",
    "read_file": "📄",
    "write_file": "✏️",
    "edit_file": "📝",
    "list_dir": "📁",
    "chatroom_send": "📤",
    "wait": "⏳",
    "yield_turn": "🔄",
    "manage_agent": "🔧",
    "end_discussion": "🏁",
    "transfer_credits": "💰",
}


def tool_icon(tool_name: str) -> str:
    """Return label for a tool (kept for backward compat with engine.py)."""
    return TOOL_LABELS.get(tool_name, tool_name)


# ── Badges & Headers ─────────────────────────────────────────

def agent_badge(agent_name: str, leader: str | None) -> str:
    """Return ' 👑' if agent is leader, else ''."""
    return " 👑" if leader == agent_name else ""


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
        '▍Benjamin 👑 [1/4]\n\n'   (direct mode)
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
    """'👑 Nanobot · grok-4  [1/4]' or '◎ Harper · grok-4-1  [2/4]'"""
    if leader == agent_name:
        tag = f"  [{idx}/{total}]" if total > 1 else ""
        return f"👑 {agent_name} · {model_short}{tag}"
    badge = agent_badge(agent_name, leader)
    tag = f"  [{idx}/{total}]" if total > 1 else ""
    return f"◎ {agent_name}{badge} · {model_short}{tag}"


def completion_msg(
    agent_name: str,
    latency: float,
    iterations: int = 1,
    tools_used: list[str] | None = None,
    leader: str | None = None,
) -> str:
    """'✓ Harper  3.2s · read×2, search' or '👑✓ Nanobot  5.1s · send×3, search×2'"""
    parts = [f"{latency:.1f}s"]
    if tools_used:
        from collections import Counter
        counts = Counter(TOOL_LABELS.get(t, t) for t in tools_used)
        tool_parts = [
            f"{name}×{cnt}" if cnt > 1 else name
            for name, cnt in counts.items()
        ]
        parts.append(", ".join(tool_parts))
    if iterations > 1:
        parts.append(f"{iterations}次迭代")
    detail = " · ".join(parts)
    icon = "👑✓" if leader == agent_name else "✓"
    return f"{icon} {agent_name}  {detail}" if detail else ""


def error_msg(agent_name: str, error: str, latency: float = 0) -> str:
    if latency:
        return f"✗ {agent_name} failed ({latency:.1f}s): {error}"
    return f"✗ {agent_name} failed: {error}"


def empty_reply_msg(agent_name: str) -> str:
    return f"✗ {agent_name}: empty reply"


def tool_in_progress_msg(header: str) -> str:
    return f"{header}– 🔧 ..."


# ── Broadcast-specific ───────────────────────────────────────

def broadcast_start_msg(agents: list[str], timeout: int, leader: str | None = None, ranks: dict[str, str] | None = None) -> str:
    """Render broadcast start banner with role indicators and rank badges."""
    total = len(agents)
    lines = [f"══ Broadcast · {total} agents · {timeout}s ══"]
    _r = ranks or {}
    if leader:
        lines.append(f"👑 {leader} ({_r.get(leader, 'pawn')})")
        members = [a for a in agents if a != leader]
        if members:
            lines.append("  ".join(f"🔹 {m} ({_r.get(m, 'pawn')})" for m in members))
    else:
        lines.append("  ".join(f"🔹 {a} ({_r.get(a, 'pawn')})" for a in agents))
    return "\n".join(lines)


def broadcast_complete_msg(
    completed: int,
    total: int,
    comm_count: int = 0,
) -> str:
    msg = f"══ Done · {completed}/{total}"
    if comm_count > 0:
        msg += f" · {comm_count} msgs"
    msg += " ══"
    return msg



def search_credits_bar(pool_status: str) -> str:
    """Format search credits for display.

    Input: 'Nanobot:2💰(0搜) | Lucas:1💰(3搜) | ...'
    Output: '🔍 Nanobot:2💰(0搜) | Lucas:1💰(3搜) | ...'
    """
    return f"🔍 {pool_status}"


def search_bar(pool: int, total: int, nodes: int) -> str:
    """Render search tree status as compact bar.

    Example: '🔍 ▰▰▱▱ 2/4 · 3 nodes'
    """
    used = total - pool
    filled = "▰" * used
    empty = "▱" * max(pool, 0)
    return f"🔍 {filled}{empty} {used}/{total} · {nodes} nodes"


def chat_chain_summary(
    history: list,
    *,
    max_preview: int = 100,
    leader: str | None = None,
) -> str:
    """Format chat history as a tree grouped by conversation threads.

    Output:
        ┄ 对话链 (8 msgs) ┄
        👑 Nanobot
        ├→ Lucas: 请搜索酒馆战棋…
        │  └← Lucas: 搜索到3条结果…
        ├→ Ares: 请分析以下数据…
        │  └← Ares: 分析完成…
        └→ All: 最终结论…
        🔹 Harper
        └→ All: 补充一点…
    """
    if not history:
        return ""

    # Build per-sender thread groups
    from collections import OrderedDict
    threads: OrderedDict[str, list] = OrderedDict()
    for msg in history:
        sender = msg.sender if hasattr(msg, "sender") else str(msg.get("sender", "?"))
        targets = msg.targets if hasattr(msg, "targets") else msg.get("targets", [])
        content = msg.content if hasattr(msg, "content") else str(msg.get("content", ""))
        if sender not in threads:
            threads[sender] = []
        threads[sender].append((sender, targets, content))

    # Build tree display
    lines = [f"┄ 对话链 ({len(history)} msgs) ┄"]

    for sender, msgs in threads.items():
        icon = "👑" if sender == leader else "🔹"
        lines.append(f"{icon} {sender}")

        for i, (_, targets, content) in enumerate(msgs):
            to = ", ".join(targets) if isinstance(targets, list) else str(targets)
            preview = content.replace("\n", " ")
            if len(preview) > max_preview:
                preview = preview[:max_preview] + "…"

            is_last = (i == len(msgs) - 1)
            branch = "└" if is_last else "├"
            lines.append(f"  {branch}→ {to}: {preview}")

    return "\n".join(lines)


def synthesis_start_msg(count: int) -> str:
    return f"━━ synthesis · {count} agent(s) ━━"


def synthesis_agent_msg(
    agent_name: str,
    model_short: str,
    is_leader: bool,
    idx: int,
    total: int,
) -> str:
    label = "synthesizing" if is_leader else "reviewing"
    icon = "👑" if is_leader else "◎"
    return f"{icon} {agent_name} {label}… ({model_short})  [{idx + 1}/{total}]"


def tool_call_line(agent_name: str, tool_name: str, short_arg: str = "") -> str:
    """Format a tool call for direct mode.

    Returns: '▸ Nanobot · search(Trump news)'
    """
    label = TOOL_LABELS.get(tool_name, tool_name)
    arg = _shorten_path(short_arg) if short_arg else ""
    return f"▸ {agent_name} · {label}({arg})"


def tool_result_line(preview: str, result_len: int) -> str:
    """Format a tool result for inline display.

    Returns: '↳ Results for: Trump… (1,234字)'
    """
    ellipsis = "…" if result_len > 80 else ""
    return f"↳ {preview}{ellipsis} ({result_len:,}字)"


# ── Tool Activity Display (broadcast mode) ───────────────────

def tool_activity_msg(
    agent_name: str,
    tool_name: str,
    args: dict,
    leader: str | None = None,
    agent_ranks: dict[str, int] | None = None,
) -> str:
    """Format a tool call for broadcast display.

    Leader:     👑▸ Nanobot · search "酒馆战棋"  → King+
    Non-leader: ▸ Lucas · search "Trump latest news"  → Pawn+
    """
    from nanobot.groupchat.display.visibility import tool_call_label

    label = TOOL_LABELS.get(tool_name, tool_name)
    prefix = "👑▸" if agent_name == leader else "  ▸"

    # Build base line
    if tool_name == "web_search":
        query = args.get("query", "")
        line = f"{prefix} {agent_name} · {label} \"{query}\""
    elif tool_name == "web_fetch":
        url = (args.get("url", "") or "")
        if len(url) > 65:
            url = url[:65] + "…"
        line = f"{prefix} {agent_name} · {label} {url}"
    elif tool_name == "exec":
        cmd = (args.get("command", "") or "")[:55]
        line = f"{prefix} {agent_name} · {label} {cmd}"
    elif tool_name == "read_file":
        path = (args.get("path", "") or "").split("/")[-1]
        line = f"{prefix} {agent_name} · {label} {path}"
    elif tool_name in ("write_file", "edit_file"):
        path = (args.get("path", "") or "").split("/")[-1]
        line = f"{prefix} {agent_name} · {label} {path}"
    elif tool_name == "list_dir":
        path = args.get("path", "") or "."
        line = f"{prefix} {agent_name} · {label} {path}"
    elif tool_name == "transfer_credits":
        fr = args.get("from_agent", "?")
        to = args.get("to_agent", "?")
        amt = args.get("amount", "?")
        line = f"{prefix} {agent_name} · {label} {fr}→{to} ×{amt}"
    elif tool_name == "manage_agent":
        action = args.get("action", "?")
        target = args.get("agent", "?")
        line = f"{prefix} {agent_name} · {label} {action}({target})"
    elif tool_name == "end_discussion":
        reason = (args.get("reason", "") or "")[:40]
        line = f"{prefix} {agent_name} · {label} {reason}"
    else:
        short = ""
        if args:
            first = list(args.values())[0]
            if isinstance(first, str):
                short = first[:40]
        suffix = f" {short}" if short else ""
        line = f"{prefix} {agent_name} · {label}{suffix}"

    # Append visibility label with actual agent names
    if agent_ranks is not None:
        sender_rank = agent_ranks.get(agent_name, 0)
        is_leader = (agent_name == leader)
        line += f"  {tool_call_label(sender_rank, agent_ranks, agent_name, is_leader=is_leader)}"

    return line


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
        return f"    └ fetched ({rlen:,}字)"
    elif tool_name == "exec":
        preview = (result or "").strip().replace("\n", " ")[:55]
        return f"    └ {preview}{'…' if rlen > 55 else ''}"
    elif tool_name == "read_file":
        lines_count = result.count("\n") + 1 if result else 0
        return f"    └ {lines_count} lines"
    elif tool_name in ("write_file", "edit_file"):
        if (result or "").startswith("Error:"):
            return f"    └ ❌ {result.split(chr(10))[0][:60]}"
        return f"    └ ✅ saved"
    elif tool_name == "list_dir":
        count = result.count("\n") if result else 0
        return f"    └ {count} entries"
    elif tool_name == "memory_palace":
        if result and "stored" in result:
            return f"    └ ✅ stored"
        elif result and "search" in result.lower():
            return f"    └ 🔍 found"
        else:
            return f"    └ ({rlen:,}字)"
    elif tool_name == "manage_agent":
        return f"    └ ✅ done"
    elif tool_name == "transfer_credits":
        return f"    └ ✅ transferred"
    elif tool_name == "end_discussion":
        return f"    └ 🏁 ended"
    else:
        return f"    └ ({rlen:,}字)"


# ── Chatroom Communication Display ───────────────────────────

def chatroom_send_msg(
    sender: str,
    to: str,
    message: str,
    *,
    max_len: int = 2000,
    leader: str | None = None,
) -> str:
    """Format an inter-agent chatroom_send.

    Leader command:
        👑 Nanobot → Lucas ━━
        请搜索酒馆战棋最新...

    Regular message:
        ┄ Lucas → Nanobot ┄
        搜索结果如下...
    """
    if len(message) > max_len:
        message = message[:max_len] + "…"
    if sender == leader:
        return (
            f"👑 {sender} → {to} ━━\n"
            f"{message}"
        )
    return (
        f"┄ {sender} → {to} ┄\n"
        f"{message}"
    )


def chatroom_wait_msg(agent: str, result: str, leader: str | None = None) -> str:
    """Format a wait tool result — short receipt notice only.

    The full message content was already shown when chatroom_send fired,
    so we only indicate who received from whom.

        ← Ares received from Harper
    """
    import re
    m = re.match(r'\[(\w+)', result)
    sender = m.group(1) if m else "teammate"
    icon = "👑←" if agent == leader else "  ←"
    return f"{icon} {agent} received from {sender}"


def user_interjection_msg(message: str, *, max_len: int = 500) -> str:
    """Format a user interjection during broadcast.

    Clean inline style:
        ── User ──
        message content
    """
    if len(message) > max_len:
        message = message[:max_len] + "…"
    return (
        f"── User ──\n"
        f"{message}"
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


# ── Leader Action Messages ───────────────────────────────────

def leader_end_msg(leader_name: str, reason: str = "") -> str:
    """Display when leader ends the discussion."""
    reason_part = f"\n    原因: {reason}" if reason else ""
    return f"👑 {leader_name} 结束讨论{reason_part}"


def leader_transfer_msg(leader_name: str, result: str) -> str:
    """Display when leader transfers search credits."""
    return f"👑🔄 {result}"


# ── Agent Status Dashboard ──────────────────────────────────

STATUS_INDICATORS: dict[str, tuple[str, str]] = {
    "thinking":     ("🟡", "Thinking..."),
    "searching":    ("🔵", "Searching"),
    "fetching":     ("🔵", "Fetching"),
    "executing":    ("🟣", "Executing"),
    "reading":      ("📖", "Reading"),
    "writing":      ("✏️", "Writing"),
    "sending":      ("🟢", "Sending"),
    "waiting":      ("⚪", "Waiting..."),
    "blocked":      ("🔴", "Blocked"),
    "interrupted":  ("⚡", "Interrupted"),
    "done":         ("✅", "Done"),
    "error":        ("❌", "Error"),
    "cancelled":    ("⬛", "Cancelled"),
}


def status_panel(
    agents: list[str],
    states: dict[str, str],
    details: dict[str, str],
    reasons: dict[str, str],
    leader: str | None = None,
) -> str:
    """Render a live status dashboard for all agents.

    Example:
        ┏━━ status ━━━━━━━━━━━━━━━━━━━┓
        ┃ 🟡 👑 Kirk      Thinking...  ┃
        ┃ 🔵    Harper    Searching... ┃
        ┃ ⚪    Verifier  Waiting...   ┃
        ┃ 🔴    Ares      Blocked: no credits ┃
        ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
    """
    lines: list[str] = []
    max_name = max((len(a) for a in agents), default=6)

    for agent in agents:
        state = states.get(agent, "thinking")
        emoji, label = STATUS_INDICATORS.get(state, ("⚪", state))
        detail = details.get(agent, "")
        reason = reasons.get(agent, "")
        badge = "👑" if agent == leader else "  "

        # Build activity text
        if state in ("blocked", "error", "cancelled") and reason:
            activity = f"{label}: {reason[:35]}"
        elif state == "done" and reason:
            activity = f"{label} ({reason[:30]})"
        elif detail:
            activity = f"{label} {detail[:30]}" if not label.endswith("...") else f"{label[:-3]} {detail[:30]}..."
        else:
            activity = label

        name_pad = agent.ljust(max_name)
        lines.append(f"┃ {emoji} {badge} {name_pad}  {activity}")

    header = "┏━━ status ━━━━━━━━━━━━━━━━━━━┓"
    footer = "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛"
    return "\n".join([header] + lines + [footer])
