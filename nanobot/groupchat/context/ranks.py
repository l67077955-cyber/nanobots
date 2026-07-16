"""Agent rank & tool-visibility **policy** (context layer).

Used by runtime (interrupt hierarchy, pool capacity) and context (who can see
tool logs in History views). Not a display concern — display only formats
labels on top of these rules.

Public API was previously under ``groupchat.display.visibility`` (shim kept).
"""

from __future__ import annotations

from loguru import logger

RANK_ORDER: dict[str, int] = {
    "basic": 0,
    "standard": 1,
    "advanced": 2,
    "expert": 3,
}

RANK_POOL_CAPACITY: dict[str, int] = {
    "basic": 2,
    "standard": 3,
    "advanced": 4,
    "expert": 5,
}

RANK_NAME: dict[int, str] = {v: k.capitalize() for k, v in RANK_ORDER.items()}

# Human-readable labels (shared by UI and announcements)
RANK_DISPLAY: dict[str, str] = {
    "basic": "基础 basic",
    "standard": "标准 standard",
    "advanced": "高级 advanced",
    "expert": "专家 expert",
}


def resolve_rank(raw: object, *, agent: str = "") -> str | None:
    """Resolve a config rank value.

    Returns:
        - A valid modern rank string (basic/standard/advanced/expert)
        - ``"basic"`` when the config omits rank (explicit schema default)
        - ``None`` when the config sets an invalid value (no silent coercion)
    """
    if raw is None:
        return "basic"
    if not isinstance(raw, str):
        logger.warning("Invalid rank type for {}: {!r}", agent or "?", raw)
        return None
    value = raw.strip().lower()
    if not value:
        return "basic"
    if value in RANK_ORDER:
        return value
    logger.warning(
        "Invalid rank {!r} for {} — must be one of {}; tier benefits disabled",
        raw, agent or "?", sorted(RANK_ORDER),
    )
    return None


def rank_interrupt_level(resolved: str | None) -> int:
    """Interrupt hierarchy int. Invalid/unresolved → 0 (lowest)."""
    if resolved is None or resolved not in RANK_ORDER:
        return 0
    return RANK_ORDER[resolved]


def rank_pool_capacity(resolved: str | None, *, leader: bool = False) -> int:
    """Conversation / search pool slots for a resolved rank.

    Invalid/unresolved agents receive the minimum (basic) slot count only.
    """
    if resolved is None or resolved not in RANK_POOL_CAPACITY:
        cap = RANK_POOL_CAPACITY["basic"]
    else:
        cap = RANK_POOL_CAPACITY[resolved]
    if leader:
        cap += 1
    return cap


def per_agent_pool_capacities(
    agents: list[str],
    registry: dict,
    leader_name: str | None,
) -> dict[str, int]:
    """Build {agent_name: pool_capacity} from registry rank config."""
    caps: dict[str, int] = {}
    for ag in agents:
        raw = registry.get(ag, {}).get("rank")
        resolved = resolve_rank(raw, agent=ag)
        caps[ag] = rank_pool_capacity(resolved, leader=(ag == leader_name))
    return caps


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
        raw = registry.get(a, {}).get("rank")
        resolved = resolve_rank(raw, agent=a)
        ranks[a] = rank_interrupt_level(resolved)
    if leader_name and leader_name in ranks:
        ranks[leader_name] = max(ranks.values()) + 1
    return ranks


def can_see_tool_call(sender_rank: int, viewer_rank: int) -> bool:
    """Viewer can see sender tool call iff viewer rank >= sender rank."""
    return viewer_rank >= sender_rank
