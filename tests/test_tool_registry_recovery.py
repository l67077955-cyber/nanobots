"""Tests for malformed tool-argument recovery."""

from __future__ import annotations

from nanobot.tools.registry import ToolRegistry


def test_recover_fragmented_exec_array() -> None:
    raw = [
        {"command": "cat > /tmp/index.html << 'EOF'\n<!DOCTYPE html>"},
        {"margin": 0, "padding": 0, "box-sizing": "border-box;"},
        {"scroll-behavior": "smooth;"},
    ]
    recovered = ToolRegistry._recover_params(raw, tool_name="exec")
    assert recovered == {"command": "cat > /tmp/index.html << 'EOF'\n<!DOCTYPE html>"}


def test_recover_write_file_from_array() -> None:
    raw = [
        {"path": "/tmp/a.html", "content": "<html></html>"},
        {"margin": 0},
    ]
    recovered = ToolRegistry._recover_params(raw, tool_name="write_file")
    assert recovered["path"] == "/tmp/a.html"
    assert recovered["content"] == "<html></html>"


def test_recover_write_file_split_across_array() -> None:
    """Path and content may be fractured into separate items for large writes."""
    raw = [
        {"path": "/tmp/big.html"},
        {"margin": 0, "padding": 0},
        {"content": "<!doctype html><h1>ok</h1>"},
    ]
    recovered = ToolRegistry._recover_params(raw, tool_name="write_file")
    assert recovered["path"] == "/tmp/big.html"
    assert recovered["content"] == "<!doctype html><h1>ok</h1>"


def test_recover_write_file_only_path() -> None:
    """Only path present: supply empty content so call is valid (model can re-write if needed)."""
    recovered = ToolRegistry._recover_params({"path": "notes.txt"}, tool_name="write_file")
    assert recovered == {"path": "notes.txt", "content": ""}


def test_recover_write_file_content_alias_text() -> None:
    recovered = ToolRegistry._recover_params({"path": "f.py", "text": "print(1)"}, tool_name="write_file")
    assert recovered["path"] == "f.py"
    assert recovered["content"] == "print(1)"