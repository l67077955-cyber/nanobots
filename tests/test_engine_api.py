"""Unit tests for GroupChatEngine public API (PLAN P1.5)."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.orchestra.engine import GroupChatEngine
from nanobot.providers.base import LLMProvider


@pytest.fixture
def mock_provider():
    """Create a minimal mock LLM provider."""
    p = MagicMock(spec=LLMProvider)
    p.get_default_model.return_value = "mock-model"
    return p


@pytest.fixture
def engine(mock_provider, tmp_path):
    """Create a GroupChatEngine with a mock provider and temp workspace."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = GroupChatConfig()
    eng = GroupChatEngine(cfg, mock_provider, ws)
    # Inject test agents into registry and state
    eng.registry = {"Alice": {"model": "gpt-4"}, "Bob": {"model": "gpt-4"}, "Carol": {"model": "gpt-4"}}
    eng._state._registry = eng.registry
    return eng


def test_rename_agent_basic(engine):
    """rename_agent should update registry, active, leader, and persist."""
    engine._active_agents = ["Alice", "Bob"]
    engine._leader = "Alice"
    engine._state.save_active(engine._active_agents)
    engine._state.save_leader(engine._leader)

    # Rename Alice → Dave
    result = engine.rename_agent("alice", "Dave")
    assert result is True
    assert "Alice" not in engine.registry
    assert "Dave" in engine.registry
    assert engine._active_agents == ["Dave", "Bob"]
    assert engine.leader == "Dave"

    # Verify persistence
    persisted_active = engine._state.load_active()
    persisted_leader = engine._state.load_leader()
    assert persisted_active == ["Dave", "Bob"]
    assert persisted_leader == "Dave"


def test_rename_agent_not_found(engine):
    """rename_agent returns False if old name not found."""
    assert engine.rename_agent("Unknown", "NewName") is False


def test_rename_agent_updates_groups(engine):
    """rename_agent should update agent groups on disk."""
    engine._state.save_groups({"team1": ["Alice", "Carol"]})

    result = engine.rename_agent("Alice", "Dave")
    assert result is True

    groups = engine._state.load_groups()
    assert groups["team1"] == ["Dave", "Carol"]


def test_active_leader_persistence_roundtrip(mock_provider, tmp_path):
    """Active agents and leader should persist and reload correctly."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = GroupChatConfig()

    engine1 = GroupChatEngine(cfg, mock_provider, ws)
    engine1.registry = {"A": {"model": "gpt-4"}, "B": {"model": "gpt-4"}, "C": {"model": "gpt-4"}}
    engine1._state._registry = engine1.registry

    engine1._active_agents = ["A", "C"]
    engine1._leader = "C"
    engine1.save_active()
    engine1._state.save_leader(engine1._leader)

    # Create a fresh engine instance with same registry (simulates restart)
    engine2 = GroupChatEngine(cfg, mock_provider, ws)
    engine2.registry = {"A": {"model": "gpt-4"}, "B": {"model": "gpt-4"}, "C": {"model": "gpt-4"}}
    engine2._state._registry = engine2.registry

    reloaded_active = engine2._state.load_active()
    reloaded_leader = engine2._state.load_leader()

    assert reloaded_active == ["A", "C"]
    assert reloaded_leader == "C"


def test_public_properties(engine):
    """All public properties should return expected values."""
    engine._active_agents = ["Alice"]
    engine._leader = "Alice"
    engine._topic = "test topic"
    engine._round = 5
    engine._debug_context = True
    engine._request_log = [{"agent": "Alice", "model": "gpt-4"}]

    assert engine.leader == "Alice"
    assert engine.topic == "test topic"
    assert engine.round_number == 5
    assert engine.debug_context is True
    assert engine.is_running is False
    assert engine.active_agents == ["Alice"]
    assert engine.request_log_size == 1
    assert engine.history_stats() == (0, 0)  # empty history


def test_history_stats(engine):
    """history_stats should count messages and characters."""
    engine._history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    count, chars = engine.history_stats()
    assert count == 2
    assert chars == len("hello") + len("world")
