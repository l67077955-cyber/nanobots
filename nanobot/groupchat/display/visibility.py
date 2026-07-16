"""Display helpers for rank labels + re-export of context rank policy.

Policy (resolve_rank, compute_agent_ranks, can_see_tool_call, …) lives in
``nanobot.groupchat.context.ranks``. This module keeps ``tool_call_label``
(UI string) and re-exports policy for backward compatibility.
"""

from __future__ import annotations

from nanobot.groupchat.context.ranks import (  # noqa: F401
    RANK_DISPLAY,
    RANK_NAME,
    RANK_ORDER,
    RANK_POOL_CAPACITY,
    can_see_tool_call,
    compute_agent_ranks,
    per_agent_pool_capacities,
    rank_interrupt_level,
    rank_pool_capacity,
    resolve_rank,
)

__all__ = [
    "RANK_ORDER",
    "RANK_POOL_CAPACITY",
    "RANK_NAME",
    "RANK_DISPLAY",
    "resolve_rank",
    "rank_interrupt_level",
    "rank_pool_capacity",
    "per_agent_pool_capacities",
    "compute_agent_ranks",
    "can_see_tool_call",
    "tool_call_label",
]


def tool_call_label(
    sender_rank: int,
    agent_ranks: dict[str, int],
    sender_name: str,
    is_leader: bool = False,
) -> str:
    """Visibility label showing actual agent names who can see the tool call.

    Examples:
        '→ Kirk'                    (leader call, only leader sees)
        '→ Harper, Kirk'            (advanced call, advanced+ sees)
        '→ Lucas, Harper, Kirk'     (basic call, all see)
    """
    if is_leader:
        return f"→ {sender_name}"
    visible = sorted(
        name for name, rank in agent_ranks.items() if rank >= sender_rank
    )
    return f"→ {', '.join(visible)}"
