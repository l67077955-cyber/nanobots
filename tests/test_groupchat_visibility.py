"""Unit tests for groupchat rank visibility helpers."""

from __future__ import annotations

import pytest

from nanobot.groupchat.display.visibility import (
    RANK_POOL_CAPACITY,
    compute_agent_ranks,
    per_agent_pool_capacities,
    rank_interrupt_level,
    rank_pool_capacity,
    resolve_rank,
)
from nanobot.groupchat.tool_policy import (
    agent_tool_enabled,
    forget_tool_enabled,
    memory_palace_tool_enabled,
)


class TestResolveRank:
    def test_modern_ranks(self):
        assert resolve_rank("basic") == "basic"
        assert resolve_rank("STANDARD") == "standard"
        assert resolve_rank("advanced") == "advanced"
        assert resolve_rank("expert") == "expert"

    def test_legacy_chess_names_are_invalid(self):
        assert resolve_rank("pawn") is None
        assert resolve_rank("knight") is None
        assert resolve_rank("bishop") is None
        assert resolve_rank("queen") is None

    def test_missing_or_blank_defaults_to_basic(self):
        assert resolve_rank(None) == "basic"
        assert resolve_rank("") == "basic"
        assert resolve_rank("   ") == "basic"

    def test_invalid_type_returns_none(self):
        assert resolve_rank(3) is None
        assert resolve_rank(["basic"]) is None


class TestRankCapacities:
    def test_pool_capacity_by_rank(self):
        assert rank_pool_capacity("basic") == RANK_POOL_CAPACITY["basic"]
        assert rank_pool_capacity("expert") == RANK_POOL_CAPACITY["expert"]
        assert rank_pool_capacity("invalid") == RANK_POOL_CAPACITY["basic"]

    def test_leader_gets_bonus_slot(self):
        assert rank_pool_capacity("basic", leader=True) == RANK_POOL_CAPACITY["basic"] + 1

    def test_per_agent_pool_capacities(self):
        registry = {
            "Kirk": {"rank": "advanced"},
            "Harper": {"rank": "standard"},
            "Lucas": {"rank": "basic"},
        }
        caps = per_agent_pool_capacities(
            ["Kirk", "Harper", "Lucas"], registry, leader_name="Kirk",
        )
        assert caps == {"Kirk": 5, "Harper": 3, "Lucas": 2}

    def test_compute_agent_ranks_leader_is_highest(self):
        registry = {
            "Kirk": {"rank": "advanced"},
            "Harper": {"rank": "standard"},
        }
        ranks = compute_agent_ranks(["Kirk", "Harper"], registry, leader_name="Kirk")
        assert ranks["Harper"] == rank_interrupt_level("standard")
        assert ranks["Kirk"] > ranks["Harper"]


class TestToolPolicy:
    def test_forget_opt_out(self):
        assert forget_tool_enabled({}) is True
        assert forget_tool_enabled({"tools": {"forget": True}}) is True
        assert forget_tool_enabled({"tools": {"forget": False}}) is False

    def test_memory_palace_opt_in(self):
        assert memory_palace_tool_enabled({}) is False
        assert memory_palace_tool_enabled({"tools": {"memory_palace": True}}) is True
        assert memory_palace_tool_enabled({"tools": {"memory_palace": False}}) is False

    def test_session_override_wins(self):
        agent = {"tools": {"forget": True, "memory_palace": False}}
        session = {"forget": False, "memory_palace": True}
        assert forget_tool_enabled(agent, session_override=session) is False
        assert memory_palace_tool_enabled(agent, session_override=session) is True

    def test_agent_tool_enabled_respects_default(self):
        assert agent_tool_enabled({}, "custom", default=False) is False
        assert agent_tool_enabled({"tools": {"custom": True}}, "custom", default=False) is True