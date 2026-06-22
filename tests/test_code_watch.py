"""Tests for the code-watch git snapshot helpers."""

from __future__ import annotations

import sys
from pathlib import Path

WATCH_DIR = Path(__file__).resolve().parents[1] / "scripts" / "code-watch"
sys.path.insert(0, str(WATCH_DIR))

from agent_insights import (  # noqa: E402
    agent_dashboard,
    architecture,
    chat_status,
    list_agents,
    module_stats,
    prompt_stack,
    recent_activity,
    runtime_snapshot,
    web_channel_config,
)
from git_snapshot import changed_files, repo_root, summary  # noqa: E402


def _repo() -> Path:
    return repo_root(Path(__file__).resolve())


def test_repo_root_finds_nanobot_src() -> None:
    root = _repo()
    assert (root / "nanobot").is_dir()


def test_summary_has_branch_and_head() -> None:
    s = summary(_repo())
    assert "branch" in s
    assert "head" in s
    assert "dirty_count" in s
    assert isinstance(s["dirty_count"], int)


def test_changed_files_returns_list() -> None:
    files = changed_files(_repo())
    assert isinstance(files, list)
    for item in files:
        assert "path" in item
        assert "status" in item


def test_module_stats_has_orchestra() -> None:
    mods = module_stats(_repo())
    ids = {m["id"] for m in mods}
    assert "orchestra" in ids
    orch = next(m for m in mods if m["id"] == "orchestra")
    assert orch["lines"] > 1000


def test_architecture_includes_runtime() -> None:
    arch = architecture(_repo(), changed_files(_repo()))
    assert "flow" in arch
    assert "runtime" in arch
    assert "mode" in arch["runtime"]
    assert "modules" in arch


def test_runtime_snapshot_structure() -> None:
    rt = runtime_snapshot()
    assert "active_agents" in rt
    assert "gateway" in rt
    assert "headless" in rt
    assert rt["mode"] in ("idle", "direct", "broadcast")


def test_list_agents_finds_harper() -> None:
    agents = list_agents()
    names = {a["name"].lower() for a in agents}
    assert "harper" in names


def test_prompt_stack_has_main_prompt() -> None:
    stack = prompt_stack(mode="direct")
    ids = [c["id"] for c in stack["components"]]
    assert "main_prompt" in ids
    assert stack["group_only_skipped"] >= 1


def test_recent_activity_returns_events() -> None:
    act = recent_activity()
    assert "events" in act
    assert isinstance(act["events"], list)


def test_agent_dashboard_combined() -> None:
    dash = agent_dashboard()
    assert "agents" in dash
    assert "prompt_stack" in dash
    assert "activity" in dash


def test_web_channel_config_structure() -> None:
    cfg = web_channel_config()
    assert "enabled" in cfg
    assert "port" in cfg


def test_chat_status_structure() -> None:
    st = chat_status()
    assert "ready" in st
    assert "web_channel" in st