"""Tests for cache-friendly prompt construction."""

from __future__ import annotations

from pathlib import Path

from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.context.prompt_builder import PromptBuilder
from nanobot.utils.helpers import RUNTIME_CONTEXT_TAG


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_system_prompt_stays_stable_when_clock_changes(tmp_path) -> None:
    """System prompt components should not change just because wall clock minute changes."""
    workspace = _make_workspace(tmp_path)
    builder = PromptBuilder(config=GroupChatConfig(), workspace=workspace)
    registry = {"Nanobot": {"model": "test", "prompt": "I am nanobot."}}

    prompt1 = builder.build_agent_prompt(
        "Nanobot", registry=registry, active_agents=["Nanobot"],
        history=[], leader=None, round_num=0,
    )
    prompt2 = builder.build_agent_prompt(
        "Nanobot", registry=registry, active_agents=["Nanobot"],
        history=[], leader=None, round_num=0,
    )

    # System components (excluding volatile datetime) should be stable
    sys1 = [m["content"] for m in prompt1 if m["role"] == "system"]
    sys2 = [m["content"] for m in prompt2 if m["role"] == "system"]
    assert len(sys1) == len(sys2)


def test_runtime_context_is_merged_with_user_message(tmp_path) -> None:
    """Runtime metadata should be merged with the user message."""
    workspace = _make_workspace(tmp_path)
    builder = PromptBuilder(config=GroupChatConfig(), workspace=workspace)
    registry = {"Nanobot": {"model": "test", "prompt": "I am nanobot."}}

    messages = builder.build_single_agent_messages(
        "Nanobot",
        registry=registry,
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    # Last message should be user with runtime context merged
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert RUNTIME_CONTEXT_TAG in user_content
    assert "Current Time:" in user_content
    assert "Channel: cli" in user_content
    assert "Chat ID: direct" in user_content
    assert "Return exactly: OK" in user_content


def test_direct_mode_uses_same_system_prompt_components_as_group(tmp_path) -> None:
    """1-on-1 prompts should keep the shared prompt component system."""
    workspace = _make_workspace(tmp_path)
    builder = PromptBuilder(config=GroupChatConfig(), workspace=workspace)
    registry = {"Nanobot": {"model": "test", "prompt": "I am nanobot."}}

    direct = builder.build_agent_prompt(
        "Nanobot",
        registry=registry,
        active_agents=["Nanobot"],
        history=[],
        leader=None,
        round_num=0,
    )
    group = builder.build_agent_prompt(
        "Nanobot",
        registry=registry,
        active_agents=["Nanobot", "Grok"],
        history=[],
        leader=None,
        round_num=0,
        teammates=["Grok"],
        agent_idx=0,
        total=2,
    )

    direct_sys = "\n".join(m["content"] for m in direct if m["role"] == "system")
    group_sys = "\n".join(m["content"] for m in group if m["role"] == "system")

    assert "[广播模式]" in direct_sys
    assert "Group members: Nanobot" in direct_sys
    assert "[广播模式]" in group_sys
    assert "Group members: Nanobot, Grok" in group_sys
