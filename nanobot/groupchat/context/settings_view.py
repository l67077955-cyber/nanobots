"""Narrow read port for Telegram /history settings panel.

Settings UI must depend on this surface — not the full GroupChatEngine
(mailbox, tool_loop, broadcast tasks, …).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from nanobot.core.history import History


@runtime_checkable
class HistorySettingsView(Protocol):
    """Read-only view the /history settings dashboard is allowed to use."""

    @property
    def history(self) -> History:
        """Shared conversation History."""
        ...

    @property
    def active_agents(self) -> list[str]:
        """Currently active agent names (may be empty)."""
        ...


def history_messages(view: HistorySettingsView | None) -> list[dict[str, Any]]:
    """Legacy sender-dicts snapshot for UI lists (not for token estimation)."""
    if not view:
        return []
    return list(view.history.to_sender_dicts())


def estimate_history_tokens(view: HistorySettingsView | None) -> int:
    """Token estimate via History.build_for_llm (same path as LLM + compress).

    Do **not** hand-map sender_dicts → role; that mislabels system messages and
    drops ``name`` fields (the bug fixed for engine._maybe_compress_history).
    """
    if not view or not view.history:
        return 0
    try:
        from nanobot.utils.helpers import estimate_message_tokens

        return int(view.history.estimate_tokens(estimate_message_tokens) or 0)
    except Exception:
        # char//4 fallback on durable content only
        return max(0, int(view.history.total_chars()) // 4)


def compiled_context_info(view: HistorySettingsView | None) -> str:
    """Per-active-agent compiled context size summary for the dashboard."""
    if not view or not view.active_agents:
        return "(engine未启动)"
    from nanobot.groupchat.context.history_preview import preview_groupchat_messages

    parts: list[str] = []
    for agent in view.active_agents:
        try:
            compiled = preview_groupchat_messages(view.history, agent=agent)
            chars = sum(len(m.get("content") or "") for m in compiled)
            parts.append(f"{agent}~{chars:,}字")
        except Exception:
            parts.append(f"{agent}:?")
    return " | ".join(parts) if parts else "(无活跃agent)"
