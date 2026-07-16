"""Per-agent tool enablement policy for groupchat."""

from __future__ import annotations

from typing import Any


def agent_tool_enabled(
    agent_cfg: dict[str, Any],
    tool_name: str,
    *,
    default: bool,
    session_override: dict[str, Any] | None = None,
) -> bool:
    """Return whether *tool_name* is enabled for an agent.

    Session overrides win over static config. Explicit ``tools`` dict entries
    win over *default* when present.
    """
    if isinstance(session_override, dict) and tool_name in session_override:
        return bool(session_override[tool_name])
    tools_cfg = agent_cfg.get("tools")
    if isinstance(tools_cfg, dict) and tool_name in tools_cfg:
        return bool(tools_cfg[tool_name])
    return default


def forget_tool_enabled(
    agent_cfg: dict[str, Any],
    *,
    session_override: dict[str, Any] | None = None,
) -> bool:
    """Forget is opt-out: enabled unless ``tools.forget`` is explicitly false."""
    return agent_tool_enabled(
        agent_cfg, "forget", default=True, session_override=session_override
    )


def memory_palace_tool_enabled(
    agent_cfg: dict[str, Any],
    *,
    session_override: dict[str, Any] | None = None,
) -> bool:
    """Memory palace is opt-in: disabled unless ``tools.memory_palace`` is true."""
    return agent_tool_enabled(
        agent_cfg, "memory_palace", default=False, session_override=session_override
    )