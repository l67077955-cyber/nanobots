"""Structural guards for the three groupchat layers."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GC = ROOT / "nanobot" / "groupchat"


def _imports_from(package_dir: Path) -> set[str]:
    found: set[str] = set()
    for path in package_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
    return found


def test_display_does_not_import_runtime() -> None:
    mods = _imports_from(GC / "display")
    bad = {m for m in mods if m.startswith("nanobot.groupchat.runtime")}
    assert not bad, f"display must not import runtime: {bad}"


def test_context_does_not_import_runtime() -> None:
    mods = _imports_from(GC / "context")
    bad = {m for m in mods if m.startswith("nanobot.groupchat.runtime")}
    assert not bad, f"context must not import runtime: {bad}"


def test_display_does_not_import_conversation() -> None:
    """View layer should not own History façade."""
    mods = _imports_from(GC / "display")
    bad = {m for m in mods if "conversation" in m}
    assert not bad, bad


def test_agent_cycle_module_exists_and_exports() -> None:
    from nanobot.groupchat.runtime.agent_cycle import AgentCycleEnv, run_agent_cycle

    assert callable(run_agent_cycle)
    assert AgentCycleEnv.__dataclass_fields__


def test_history_is_sole_commit_api() -> None:
    from nanobot.core.history import History

    h = History()
    assert h.commit_turn("用户", "") == ""
    assert h.commit_turn("用户", "hello") == "hello"
    assert len(h) == 1
    assert h.latest_user_content() == "hello"


def test_commit_agent_turn_writes_only_history() -> None:
    from nanobot.core.history import History
    from nanobot.groupchat.runtime.working_memory import commit_agent_turn

    class Eng:
        def __init__(self) -> None:
            self.history = History()
            self.persisted: list[tuple[str, str]] = []

        def _persist_after_history_write(self, sender: str, content: str) -> None:
            self.persisted.append((sender, content))

    eng = Eng()
    out = commit_agent_turn(eng, "Harper", "hi", tool_calls_detail=None)
    assert out == "hi"
    assert len(eng.history) == 1
    assert eng.persisted == [("Harper", "hi")]


def test_conversation_port_uses_history() -> None:
    from nanobot.core.history import History
    from nanobot.groupchat.context.conversation import HistoryConversation

    h = History()
    writes: list[tuple[str, str]] = []
    conv = HistoryConversation(history=h, on_write=lambda s, c: writes.append((s, c)))
    conv.commit("用户", "q")
    assert h.latest_user_content() == "q"
    assert writes == [("用户", "q")]
