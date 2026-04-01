"""display.py — 所有群聊显示格式化函数（纯格式化，无业务逻辑）。

所有发给用户看的消息格式都在这里定义。其他文件通过 import display as _d 调用。

函数分类：
    agent_badge / agent_header    — agent 名称 + 图标
    thinking_msg / completion_msg — 状态提示（开始思考、完成）
    broadcast_start/complete_msg  — 广播轮次的开始/结束横幅
    tool_activity_msg             — 工具调用显示（搜索、读文件等）
    tool_result_brief             — 工具结果摘要
    chatroom_send_msg             — agent 间消息显示
    chatroom_wait_msg             — 等待消息回执

⚠️ 这里是纯格式化 — 不要在这里加业务逻辑。
"""

from __future__ import annotations


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
    "manage_agent": "manage",
    "end_discussion": "end",
    "transfer_credits": "transfer",
}



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
        '▍Benjamin 👑 [1/4]\\n\\n'   (group/serial)
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
        parts.append(f"{iterations}轮")
    detail = " · ".join(parts)
    icon = "👑✓" if leader == agent_name else "✓"
    return f"{icon} {agent_name}  {detail}" if detail else ""


def error_msg(agent_name: str, error: str, latency: float = 0) -> str:
    if latency:
        return f"✗ {agent_name} failed ({latency:.1f}s): {error}"
    return f"✗ {agent_name} failed: {error}"



def tool_in_progress_msg(header: str) -> str:
    return f"{header}– 🔧 ..."


# ── Broadcast-specific ───────────────────────────────────────

def broadcast_start_msg(agents: list[str], timeout: int, leader: str | None = None) -> str:
    """Render broadcast start banner with role indicators.

    ┏━━ broadcast · 4 agents · 200s ━━┓
    │ 👑 Nanobot (leader)              │
    │ 🔹 Lucas  🔹 Benjamin  🔹 Ares  │
    └──────────────────────────────────┘
    """
    total = len(agents)
    lines = [f"┏━━ broadcast · {total} agents · {timeout}s ━━┓"]
    if leader:
        lines.append(f"│ 👑 {leader}")
        members = [a for a in agents if a != leader]
        if members:
            lines.append(f"│ {'  '.join('🔹 ' + a for a in members)}")
    else:
        lines.append(f"│ {'  '.join('🔹 ' + a for a in agents)}")
    lines.append("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    return "\n".join(lines)


def broadcast_complete_msg(
    completed: int,
    total: int,
    comm_count: int = 0,
) -> str:
    msg = f"┗━━ done · {completed}/{total}"
    if comm_count > 0:
        msg += f" · {comm_count} msgs"
    msg += " ━━┛"
    return msg






def tool_call_line(agent_name: str, tool_name: str, short_arg: str = "") -> str:
    """Format a tool call for serial/orchestra mode.

    Returns: '▸ Nanobot · search(Trump news)'
    """
    label = TOOL_LABELS.get(tool_name, tool_name)
    arg = _shorten_path(short_arg) if short_arg else ""
    return f"▸ {agent_name} · {label}({arg})"

# ── Tool Activity Display (broadcast mode) ───────────────────

def tool_activity_msg(
    agent_name: str,
    tool_name: str,
    args: dict,
    leader: str | None = None,
) -> str:
    """Format a tool call for broadcast display.

    Leader:     👑▸ Nanobot · search "酒馆战棋"
    Non-leader: ▸ Lucas · search "Trump latest news"
    """
    label = TOOL_LABELS.get(tool_name, tool_name)
    prefix = "👑▸" if agent_name == leader else "  ▸"
    if tool_name == "web_search":
        query = args.get("query", "")
        return f"{prefix} {agent_name} · {label} \"{query}\""
    elif tool_name == "web_fetch":
        url = (args.get("url", "") or "")
        if len(url) > 65:
            url = url[:65] + "…"
        return f"{prefix} {agent_name} · {label} {url}"
    elif tool_name == "exec":
        cmd = (args.get("command", "") or "")[:55]
        return f"{prefix} {agent_name} · {label} {cmd}"
    elif tool_name == "read_file":
        path = (args.get("path", "") or "").split("/")[-1]
        return f"{prefix} {agent_name} · {label} {path}"
    elif tool_name in ("write_file", "edit_file"):
        path = (args.get("path", "") or "").split("/")[-1]
        return f"{prefix} {agent_name} · {label} {path}"
    elif tool_name == "list_dir":
        path = args.get("path", "") or "."
        return f"{prefix} {agent_name} · {label} {path}"
    elif tool_name == "transfer_credits":
        fr = args.get("from_agent", "?")
        to = args.get("to_agent", "?")
        amt = args.get("amount", "?")
        return f"{prefix} {agent_name} · {label} {fr}→{to} ×{amt}"
    elif tool_name == "manage_agent":
        action = args.get("action", "?")
        target = args.get("agent", "?")
        return f"{prefix} {agent_name} · {label} {action}({target})"
    elif tool_name == "end_discussion":
        reason = (args.get("reason", "") or "")[:40]
        return f"{prefix} {agent_name} · {label} {reason}"
    else:
        short = ""
        if args:
            first = list(args.values())[0]
            if isinstance(first, str):
                short = first[:40]
        suffix = f" {short}" if short else ""
        return f"{prefix} {agent_name} · {label}{suffix}"


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
    else:
        return f"    └ ({rlen:,}字)"


# ── Leader Update Display ────────────────────────────────────

def leader_update_msg(
    leader_name: str,
    content: str,
    *,
    max_len: int = 800,
) -> str:
    """Format leader reasoning/analysis for user display.

    Shows the leader's thinking with a distinct visual banner:

        👑 Kirk ━━━━━━━━
        收到 Verifier 报告，分析结论如下...
        ━━━━━━━━━━━━━━━
    """
    if len(content) > max_len:
        content = content[:max_len] + "…"
    return (
        f"👑 {leader_name} ━━━━━━━━\n"
        f"{content}\n"
        f"━━━━━━━━━━━━━━━"
    )


# ── Chatroom Communication Display ───────────────────────────

def chatroom_send_msg(
    sender: str,
    to: str,
    message: str,
    *,
    max_len: int = 500,
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

