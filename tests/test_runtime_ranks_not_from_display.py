"""Runtime rank policy must come from context, not display."""

from __future__ import annotations

import ast
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "nanobot" / "groupchat" / "runtime"


def test_runtime_does_not_import_display_visibility():
    bad: list[str] = []
    for py in RUNTIME.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "nanobot.groupchat.display.visibility" or (
                    node.module.startswith("nanobot.groupchat.display.visibility")
                ):
                    bad.append(f"{py.relative_to(RUNTIME.parent.parent.parent)}: {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "display.visibility" in alias.name:
                        bad.append(f"{py.name}: {alias.name}")
    assert bad == [], "runtime must use context.ranks, not display.visibility:\n" + "\n".join(bad)
