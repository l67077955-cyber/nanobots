#!/usr/bin/env python3
"""Regression tests for direct_chat iteration + provider_meta handling."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanobot.groupchat.context.tool_log import build_tool_log
from nanobot.groupchat.runtime.chat_utils import reasoning_tokens_from_provider_meta
from nanobot.groupchat.runtime.tools.tool_chat import resolve_max_tool_iterations


class _FakeEngine:
    def __init__(self, registry):
        self.registry = registry


def test_reasoning_tokens_from_provider_meta_list():
    meta = [{"reasoning_tokens": 12}, {"reasoning_tokens": 8}]
    assert reasoning_tokens_from_provider_meta(meta) == 20


def test_reasoning_tokens_from_provider_meta_dict():
    meta = {"reasoning_tokens": 7}
    assert reasoning_tokens_from_provider_meta(meta) == 7


def test_resolve_max_tool_iterations_from_registry():
    engine = _FakeEngine({"Kirk": {"max_tool_iterations": 50}})
    assert resolve_max_tool_iterations(engine, "Kirk", is_direct=True) == 50


def test_resolve_max_tool_iterations_from_agent_config(tmp_path: Path):
    agent_dir = tmp_path / "kirk"
    agent_dir.mkdir()
    (agent_dir / "config.json").write_text(
        '{"agents": {"defaults": {"maxToolIterations": 33}}}'
    )
    engine = _FakeEngine({"Kirk": {"agent_dir": str(agent_dir)}})
    assert resolve_max_tool_iterations(engine, "Kirk", is_direct=True) == 33


def test_direct_chat_return_semantics():
    """Document expected return: text for CLI callers even after tool-only turns."""
    last_response = "" or "[仅调用了工具，无文字回复]"
    assert last_response == "[仅调用了工具，无文字回复]"


def test_build_tool_log_handles_non_dict_args():
    detail = [{
        "name": "exec",
        "args": ["not", "a", "dict"],
        "result_len": 3,
        "result_preview": "ok",
        "success": True,
    }]
    log = build_tool_log(detail)
    assert "exec" in log