"""CLI helpers for discovering and recovering groupchat logs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

_NANOBOT_DIR = Path.home() / ".nanobot"
_SESSIONS_DIR = _NANOBOT_DIR / "collab-sessions"


def resolve_session_dir(session_id: str | None = None) -> Path | None:
    """Resolve a collab session directory from explicit id or known pointers."""
    if session_id:
        explicit = _SESSIONS_DIR / session_id
        if explicit.is_dir():
            return explicit
        if not session_id.startswith("gc-"):
            prefixed = _SESSIONS_DIR / f"gc-{session_id}"
            if prefixed.is_dir():
                return prefixed
        return None

    current_file = _NANOBOT_DIR / "current_session.json"
    if current_file.exists():
        try:
            data = json.loads(current_file.read_text())
            path = Path(str(data.get("session_dir") or ""))
            if path.is_dir():
                return path
            sid = str(data.get("session_id") or "")
            if sid:
                candidate = _SESSIONS_DIR / sid
                if candidate.is_dir():
                    return candidate
        except Exception:
            pass

    history_file = _NANOBOT_DIR / "chat_history.json"
    if history_file.exists():
        try:
            data = json.loads(history_file.read_text())
            path = Path(str(data.get("session_dir") or ""))
            if path.is_dir():
                return path
        except Exception:
            pass

    if not _SESSIONS_DIR.is_dir():
        return None
    candidates = sorted(
        (p for p in _SESSIONS_DIR.iterdir() if p.is_dir() and p.name.startswith("gc-")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_session_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "session.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def show_session_summary(console: Console, session_dir: Path) -> None:
    events = _load_session_events(session_dir)
    messages = [e for e in events if e.get("type") == "message"]
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    starts = [e for e in events if e.get("type") == "session_start"]

    table = Table(title="Nanobot Session Logs")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("session_id", session_dir.name)
    table.add_row("session_dir", str(session_dir))
    if starts:
        meta = starts[0]
        table.add_row("topic", str(meta.get("topic") or ""))
        table.add_row("mode", str(meta.get("mode") or ""))
        table.add_row("leader", str(meta.get("leader") or ""))
        table.add_row("agents", ", ".join(meta.get("agents") or []))
        table.add_row("started_at", str(meta.get("ts") or ""))
    table.add_row("messages", str(len(messages)))
    table.add_row("tool_calls", str(len(tool_calls)))
    table.add_row("session.jsonl", str(session_dir / "session.jsonl"))
    table.add_row("chat_log.txt", str(session_dir / "chat_log.txt"))
    table.add_row("gateway.log", str(_NANOBOT_DIR / "logs" / "gateway.log"))
    table.add_row("chat_history.json", str(_NANOBOT_DIR / "chat_history.json"))
    console.print(table)


def print_timeline(
    console: Console,
    session_dir: Path,
    *,
    last: int = 30,
    include_tools: bool = False,
) -> None:
    events = _load_session_events(session_dir)
    shown = 0
    for event in reversed(events):
        etype = event.get("type")
        if etype == "message":
            agent = event.get("agent", "?")
            ts = event.get("ts", "")
            content = str(event.get("content") or "")
            preview = content.replace("\n", " ")[:160]
            console.print(f"[dim]{ts}[/dim] [bold]{agent}[/bold]: {preview}")
            shown += 1
        elif include_tools and etype in {"tool_call", "tool_result"}:
            agent = event.get("agent", "?")
            ts = event.get("ts", "")
            if etype == "tool_call":
                tool = event.get("tool", "?")
                console.print(f"[dim]{ts}[/dim] [yellow]{agent}[/yellow] → {tool}()")
            else:
                tool = event.get("tool", "?")
                preview = str(event.get("result_preview") or "")
                if not preview:
                    preview = f"({event.get('result_len', 0)} chars)"
                preview = preview.replace("\n", " ")[:120]
                console.print(f"[dim]{ts}[/dim] [green]{agent}[/green] ← {tool}: {preview}")
            shown += 1
        if shown >= last:
            break


def recover_conversation(session_dir: Path) -> str:
    """Build a readable markdown transcript from session + chat history."""
    lines: list[str] = [f"# Session {session_dir.name}", ""]
    events = _load_session_events(session_dir)
    seen_messages: set[tuple[str, str, str]] = set()

    for event in events:
        if event.get("type") != "message":
            continue
        agent = str(event.get("agent") or "?")
        ts = str(event.get("ts") or "")
        content = str(event.get("content") or "").strip()
        if not content:
            continue
        key = (ts, agent, content[:200])
        if key in seen_messages:
            continue
        seen_messages.add(key)
        lines.append(f"## {agent} ({ts})")
        lines.append("")
        lines.append(content)
        lines.append("")

    history_file = _NANOBOT_DIR / "chat_history.json"
    if history_file.exists():
        try:
            snapshot = json.loads(history_file.read_text())
            if Path(str(snapshot.get("session_dir") or "")) == session_dir:
                lines.append("---")
                lines.append("")
                lines.append("## Live chat snapshot")
                lines.append("")
                for msg in snapshot.get("history", []):
                    sender = str(msg.get("sender") or "?")
                    content = str(msg.get("content") or "").strip()
                    if content:
                        lines.append(f"**{sender}**: {content[:500]}")
                        lines.append("")
        except Exception:
            pass

    return "\n".join(lines).strip() + "\n"


def grep_session(session_dir: Path, pattern: str, *, limit: int = 20) -> list[str]:
    import re

    rx = re.compile(pattern, re.IGNORECASE)
    hits: list[str] = []
    for event in _load_session_events(session_dir):
        blob = json.dumps(event, ensure_ascii=False)
        if rx.search(blob):
            hits.append(blob[:300])
            if len(hits) >= limit:
                break
    return hits