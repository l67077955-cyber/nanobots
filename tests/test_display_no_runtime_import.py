"""Display layer must not import groupchat.runtime (parent boundary)."""

from __future__ import annotations

import ast
from pathlib import Path

DISPLAY = Path(__file__).resolve().parents[1] / "nanobot" / "groupchat" / "display"


def _imports_runtime(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("nanobot.groupchat.runtime"):
                bad.append(f"{path.name}: from {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nanobot.groupchat.runtime"):
                    bad.append(f"{path.name}: import {alias.name}")
    return bad


def test_display_package_does_not_import_runtime():
    offenders: list[str] = []
    for py in DISPLAY.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        offenders.extend(_imports_runtime(py))
    assert offenders == [], "display must not import runtime:\n" + "\n".join(offenders)
