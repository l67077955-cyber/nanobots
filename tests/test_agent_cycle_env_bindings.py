"""Guard: agent_cycle free names from extraction must stay bound."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from nanobot.groupchat.runtime.agent_cycle import AgentCycleEnv, run_agent_cycle
from nanobot.groupchat.runtime.chat_utils import log_request


def test_agent_cycle_env_has_ranks_map() -> None:
    fields = {f.name for f in AgentCycleEnv.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert "ranks_map" in fields
    assert "agent_ranks" in fields


def test_agent_cycle_imports_log_request_and_random() -> None:
    src = Path("nanobot/groupchat/runtime/agent_cycle.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for n in node.names:
                imported.add(n.asname or n.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for n in node.names:
                imported.add(n.asname or n.name)
    assert "random" in imported
    assert "log_request" in imported
    # log_request symbol is the chat_utils function
    assert callable(log_request)


def test_run_agent_cycle_binds_ranks_map_from_env() -> None:
    src = Path("nanobot/groupchat/runtime/agent_cycle.py").read_text(encoding="utf-8")
    assert "ranks_map = env.ranks_map" in src
    assert "ranks_map.get" in src


def test_broadcast_passes_ranks_map() -> None:
    import re
    src = Path("nanobot/groupchat/runtime/broadcast.py").read_text(encoding="utf-8")
    m = re.search(r"_cycle_env = AgentCycleEnv\([\s\S]*?\n    \)", src)
    assert m, "AgentCycleEnv construction not found"
    assert "ranks_map=ranks_map" in m.group(0)
    m2 = re.search(r"view = BroadcastView\([\s\S]*?\n    \)", src)
    assert m2 and "ranks_map" not in m2.group(0)
