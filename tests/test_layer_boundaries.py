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


def test_settings_panel_does_not_import_live_display_stack() -> None:
    """Telegram settings UI is not the live chat view stack."""
    import ast
    from pathlib import Path

    settings_files = [
        ROOT / "nanobot/channels/telegram/settings_history_panel.py",
        ROOT / "nanobot/channels/telegram/callbacks/history.py",
        ROOT / "nanobot/channels/telegram/commands/settings.py",
    ]
    forbidden = {
        "nanobot.groupchat.display.broadcast_view",
        "nanobot.groupchat.display.streaming",
        "nanobot.groupchat.display.status_tracker",
    }
    bad: list[str] = []
    for path in settings_files:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in forbidden:
                bad.append(f"{path.name}: {node.module}")
    assert not bad, bad


def test_context_config_modules_importable() -> None:
    from nanobot.groupchat.context.gc_config import GroupChatConfig
    from nanobot.groupchat.config import GroupChatConfig as Pub
    from nanobot.groupchat.context.history_preview import preview_groupchat_messages
    from nanobot.core.history import History

    assert Pub is GroupChatConfig
    h = History()
    h.commit_turn("用户", "hi")
    msgs = preview_groupchat_messages(h, agent="Harper")
    assert isinstance(msgs, list)


def test_history_settings_view_protocol() -> None:
    from nanobot.core.history import History
    from nanobot.groupchat.context.settings_view import HistorySettingsView, history_messages

    class Stub:
        def __init__(self) -> None:
            self.history = History()
            self.history.commit_turn("用户", "x")

        @property
        def active_agents(self) -> list[str]:
            return ["Harper"]

    s = Stub()
    assert isinstance(s, HistorySettingsView)
    assert history_messages(s)[0]["content"] == "x"


def test_settings_panel_accepts_view_not_only_engine() -> None:
    from nanobot.core.history import History
    from nanobot.channels.telegram.settings_history_panel import collect_live_metrics

    class Stub:
        def __init__(self) -> None:
            self.history = History()

        @property
        def active_agents(self) -> list[str]:
            return []

    m = collect_live_metrics(Stub())
    assert "current_msgs" in m
    assert m["current_msgs"] == 0


def test_tool_catalog_shared() -> None:
    from nanobot.groupchat.runtime.tool_catalog import TOOL_NAMES
    from nanobot.groupchat.runtime.engine import GroupChatEngine

    assert GroupChatEngine.TOOL_NAMES == TOOL_NAMES
    assert "web_search" in TOOL_NAMES


def test_control_port_extends_settings_view() -> None:
    from nanobot.groupchat.context.settings_view import HistorySettingsView
    from nanobot.groupchat.runtime.control_port import GroupChatControlPort

    # Protocols with attribute members reject issubclass(); check MRO instead.
    assert HistorySettingsView in GroupChatControlPort.__mro__


def test_settings_panel_does_not_import_engine() -> None:
    import ast
    from pathlib import Path

    src = Path("nanobot/channels/telegram/settings_history_panel.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
    bad = [m for m in imports if "engine" in m.split(".")[-1] or m.endswith(".engine")]
    assert not bad, f"settings panel must not import engine: {bad}"
    assert any("settings_view" in m for m in imports)


def test_collab_bus_round_log_not_history() -> None:
    """Delivery log API must not be named history (History is core.history)."""
    import ast
    from pathlib import Path

    src = Path("nanobot/groupchat/runtime/mailbox.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MailboxHub":
            names = []
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "history":
                    # allow only if not property public history
                    names.append(item.name)
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "history":
                    names.append(item.name)
            assert "history" not in names, "MailboxHub must not expose history()"
            break
    assert "def round_log" in src


def test_chatroom_send_tools_type_collab_bus() -> None:
    from nanobot.groupchat.runtime.collab_bus import CollabBus
    from nanobot.groupchat.runtime.tools.chatroom_tools import ChatroomSendTool, WaitTool
    import inspect
    from typing import get_type_hints

    hints = get_type_hints(ChatroomSendTool.__init__)
    assert hints.get("mailbox") is CollabBus
    hints_w = get_type_hints(WaitTool.__init__)
    assert hints_w.get("mailbox") is CollabBus
