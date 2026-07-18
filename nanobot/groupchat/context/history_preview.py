"""Read-only History projections for settings / log UIs.

Channel layers (Telegram) should call these helpers instead of re-implementing
``History.build_for_groupchat`` when previewing context.
"""

from __future__ import annotations

from typing import Any

from nanobot.core.history import History
from nanobot.groupchat.context.ranks import compute_agent_ranks


def preview_groupchat_messages(
    history: History | list[dict[str, Any]],
    *,
    agent: str,
    registry: dict[str, Any] | None = None,
    leader: str | None = None,
    agent_ranks: dict[str, int] | None = None,
    max_chars: int = 0,
) -> list[dict[str, Any]]:
    """Project shared History into the LLM message list for *agent*.

    Accepts either a ``History`` instance or legacy sender-dicts.
    """
    if isinstance(history, History):
        h = history
    else:
        h = History.from_sender_dicts(list(history or []))

    ranks = agent_ranks
    if ranks is None and registry is not None:
        ranks = compute_agent_ranks(list(registry.keys()), registry, leader)

    return h.build_for_groupchat(
        current_agent=agent,
        agent_ranks=ranks,
        max_chars=max_chars,
    )


def preview_raw_and_compiled(
    history: History | list[dict[str, Any]],
    *,
    agent: str,
    registry: dict[str, Any] | None = None,
    leader: str | None = None,
) -> dict[str, Any]:
    """Snapshot used by Telegram log/context debug panels."""
    if isinstance(history, History):
        raw = history.to_sender_dicts()
        h = history
    else:
        raw = list(history or [])
        h = History.from_sender_dicts(raw)

    ranks = None
    if registry is not None:
        ranks = compute_agent_ranks(list(registry.keys()), registry, leader)

    compiled = h.build_for_groupchat(
        current_agent=agent,
        agent_ranks=ranks,
    )
    return {
        "raw_count": len(raw),
        "raw_chars": h.total_chars(),
        "compiled": compiled,
        "compiled_count": len(compiled),
        "agent": agent,
        "agent_ranks": ranks or {},
    }


def estimate_history_tokens(
    history: History | list[dict[str, Any]],
    *,
    fn=None,
) -> int:
    """Estimate tokens for History (or legacy sender-dicts) via build_for_llm.

    Prefer passing a ``History`` instance. Sender-dicts are rehydrated first so
    role/name mapping stays consistent with compress + LLM paths.
    """
    if isinstance(history, History):
        h = history
    else:
        h = History.from_sender_dicts(list(history or []))
    if not h:
        return 0
    if fn is None:
        from nanobot.utils.helpers import estimate_message_tokens

        fn = estimate_message_tokens
    try:
        return int(h.estimate_tokens(fn) or 0)
    except Exception:
        return max(0, int(h.total_chars()) // 4)

