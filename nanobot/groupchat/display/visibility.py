"""Single source of truth for tool call visibility.

All visibility logic lives here. Display layer and message converter
MUST import from this module — no inline rank comparisons elsewhere.
"""

from __future__ import annotations

RANK_ORDER: dict[str, int] = {
    "basic": 0,
    "standard": 1,
    "advanced": 2,
    "expert": 3,
}

RANK_NAME: dict[int, str] = {v: k.capitalize() for k, v in RANK_ORDER.items() if k in ("basic", "standard", "advanced", "expert")}

# Friendly display labels for UI and announcements
RANK_DISPLAY: dict[str, str] = {
    "basic": "基础 basic",
    "standard": "标准 standard",
    "advanced": "高级 advanced",
    "expert": "专家 expert",
}


def compute_agent_ranks(
    agents: list[str],
    registry: dict,
    leader_name: str | None,
) -> dict[str, int]:
    """Compute {agent_name: rank_int} from registry config.

    Leader always gets the highest rank (max + 1).
    """
    ranks: dict[str, int] = {}
    for a in agents:
        r = registry.get(a, {}).get("rank", "basic")
        ranks[a] = RANK_ORDER.get(r, 0) if isinstance(r, str) else int(r)
    if leader_name and leader_name in ranks:
        ranks[leader_name] = max(ranks.values()) + 1
    return ranks


def can_see_tool_call(sender_rank: int, viewer_rank: int) -> bool:
    """Viewer can see sender's tool call iff viewer rank >= sender rank."""
    return viewer_rank >= sender_rank


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
