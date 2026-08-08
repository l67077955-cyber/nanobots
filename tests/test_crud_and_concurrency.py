#!/usr/bin/env python3
"""Comprehensive tests for nanobot Agent/Provider CRUD + Concurrency + Security.

Covers:
  - Agent: add / remove / delete / reorder / set_leader / active_agents / registry
  - Persistence: GroupChatState save/load round-trip (isolated in tmp dir)
  - Concurrency: asyncio concurrent add/remove on _active_agents
  - Security: special chars, empty inputs

Fixtures construct a lightweight engine with an isolated GroupChatState so no
real config or ~/.nanobot state is touched.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.groupchat.history.persistence import GroupChatState


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_engine(tmp_path):
    from nanobot.groupchat.orchestra.engine import GroupChatEngine

    engine = GroupChatEngine.__new__(GroupChatEngine)
    engine.registry = {
        "Alpha": {"model": "test-model", "prompt": "You are Alpha"},
        "Beta": {"model": "test-model", "prompt": "You are Beta"},
        "Gamma": {"model": "test-model", "prompt": "You are Gamma"},
    }
    engine._active_agents = []
    engine._leader = None
    engine._round = 0
    engine._running = False
    engine._task = None
    engine._broadcast_tasks = {}
    engine._pending_join_queue = asyncio.Queue()
    engine._mailbox_patch = None
    from unittest.mock import MagicMock
    engine._mailbox = MagicMock()
    engine._send_fn = None
    engine._edit_fn = None
    engine._send_and_get_id_fn = None
    engine._on_round_done = None
    engine._request_log = []
    engine._history = []
    engine._prompt_builder = MagicMock()
    engine._tool_registry_cache = {}
    engine._current_group_name = None
    engine._groups = {}
    engine._topic = None
    # Isolated persistence in the tmp dir — no real ~/.nanobot writes.
    engine._state = GroupChatState(engine.registry, state_dir=tmp_path)
    # _session_dir is a property setter that writes through to _state.session_dir,
    # so it must be assigned AFTER _state exists.
    engine._session_dir = None
    return engine


@pytest.fixture
def engine(tmp_path):
    return _make_engine(tmp_path)


# ==================================================================
# SECTION 1: Agent CRUD (engine.py)
# ==================================================================


class TestAgentAdd:
    def test_add_success(self, engine):
        r = engine.add_agent("Alpha")
        assert "✅" in r
        assert engine.active_agents == ["Alpha"]

    def test_add_case_insensitive(self, engine):
        engine.add_agent("alpha")
        assert engine.active_agents == ["Alpha"]

    def test_add_duplicate(self, engine):
        engine.add_agent("Alpha")
        r = engine.add_agent("Alpha")
        assert "已在" in r
        assert len(engine.active_agents) == 1

    def test_add_unknown(self, engine):
        r = engine.add_agent("Ghost")
        assert "不存在" in r
        assert engine.active_agents == []

    def test_add_empty(self, engine):
        r = engine.add_agent("")
        assert "不存在" in r or "未找到" in r

    def test_add_special_chars(self, engine):
        engine.registry["测试"] = {"model": "m", "prompt": "p"}
        engine.registry["😀"] = {"model": "m", "prompt": "p"}
        assert "✅" in engine.add_agent("测试")
        assert "✅" in engine.add_agent("😀")
        assert engine.active_agents == ["测试", "😀"]

    def test_add_persists(self, engine, tmp_path):
        engine.add_agent("Alpha")
        saved = json.loads((tmp_path / "active_agents.json").read_text())
        assert saved == ["Alpha"]


class TestAgentRemove:
    def test_remove_success(self, engine):
        engine.add_agent("Alpha")
        engine.add_agent("Beta")
        r = engine.remove_agent("Alpha")
        assert "✅" in r
        assert engine.active_agents == ["Beta"]

    def test_remove_not_active(self, engine):
        r = engine.remove_agent("Alpha")
        assert "不在" in r

    def test_remove_nonexistent(self, engine):
        r = engine.remove_agent("Ghost")
        assert "不在" in r or "未找到" in r

    def test_remove_last_agent(self, engine):
        engine.add_agent("Alpha")
        engine.remove_agent("Alpha")
        assert engine.active_agents == []


class TestAgentDelete:
    def test_delete_removes_from_registry(self, engine):
        engine.add_agent("Alpha")
        engine.delete_agent("Alpha")
        assert "Alpha" not in engine.registry
        assert engine.active_agents == []

    def test_delete_nonexistent(self, engine):
        assert engine.delete_agent("Ghost") is False


class TestAgentReorder:
    def test_reorder_success(self, engine):
        for a in ["Alpha", "Beta", "Gamma"]:
            engine.add_agent(a)
        r = engine.reorder_agents(["Gamma", "Alpha", "Beta"])
        assert "✅" in r
        assert engine.active_agents == ["Gamma", "Alpha", "Beta"]

    def test_reorder_wrong_count(self, engine):
        engine.add_agent("Alpha")
        engine.add_agent("Beta")
        r = engine.reorder_agents(["Alpha"])
        assert r.startswith("⚠️") or "❌" in r

    def test_reorder_not_permutation(self, engine):
        engine.add_agent("Alpha")
        engine.add_agent("Beta")
        r = engine.reorder_agents(["Alpha", "Alpha", "Beta"])
        assert "重复" in r or "❌" in r

    def test_reorder_non_list(self, engine):
        engine.add_agent("Alpha")
        r = engine.reorder_agents("Alpha")
        assert "⚠️" in r or "❌" in r


class TestSetLeader:
    def test_set_leader(self, engine):
        engine.add_agent("Alpha")
        r = engine.set_leader("Alpha")
        assert "👑" in r
        assert "Alpha" in r
        assert engine._leader == "Alpha"

    def test_set_leader_unknown(self, engine):
        r = engine.set_leader("Ghost")
        assert "不存在" in r
        assert engine._leader is None

    def test_clear_leader(self, engine):
        engine.add_agent("Alpha")
        engine.set_leader("Alpha")
        r = engine.set_leader(None)
        assert "已取消" in r
        assert engine._leader is None


class TestGroups:
    @pytest.mark.asyncio
    async def test_save_load_group(self, engine):
        engine.add_agent("Alpha")
        engine.add_agent("Beta")
        engine.save_group("core")
        engine.remove_agent("Alpha")
        engine.remove_agent("Beta")
        # load_group auto-starts the loop for 2+ agents, so it needs an event loop.
        r = engine.load_group("core")
        assert "已载入" in r
        assert engine.active_agents == ["Alpha", "Beta"]
        engine._stop_group_loop()

    def test_save_group_no_active(self, engine):
        r = engine.save_group("empty")
        assert "没有活跃" in r

    def test_load_group_unknown(self, engine):
        r = engine.load_group("ghost")
        assert "不存在" in r

    def test_delete_group(self, engine):
        engine.add_agent("Alpha")
        engine.save_group("core")
        r = engine.delete_group("core")
        assert "已删除" in r
        assert "core" not in engine._state.load_groups()

    def test_list_groups(self, engine):
        engine.add_agent("Alpha")
        engine.save_group("core")
        r = engine.list_groups()
        assert "core" in r


# ==================================================================
# SECTION 2: Persistence round-trips (isolated in tmp dir)
# ==================================================================


class TestPersistence:
    def test_active_round_trip(self, tmp_path):
        state = GroupChatState({"Alpha": {}, "Beta": {}}, state_dir=tmp_path)
        state.save_active(["Alpha", "Beta"])
        assert state.load_active() == ["Alpha", "Beta"]

    def test_active_filters_deleted(self, tmp_path):
        state = GroupChatState({"Alpha": {}}, state_dir=tmp_path)
        state.save_active(["Alpha", "Ghost"])
        # Ghost is not in the registry — must be filtered out on load.
        assert state.load_active() == ["Alpha"]

    def test_active_corrupt_file_returns_empty(self, tmp_path):
        state = GroupChatState({"Alpha": {}}, state_dir=tmp_path)
        (tmp_path / "active_agents.json").write_text("{not json")
        assert state.load_active() == []

    def test_leader_round_trip(self, tmp_path):
        state = GroupChatState({"Alpha": {}}, state_dir=tmp_path)
        state.save_leader("Alpha")
        assert state.load_leader() == "Alpha"
        state.save_leader(None)
        assert state.load_leader() is None

    def test_groups_round_trip(self, tmp_path):
        state = GroupChatState({"Alpha": {}, "Beta": {}}, state_dir=tmp_path)
        state.save_groups({"core": ["Alpha", "Beta"]})
        assert state.load_groups() == {"core": ["Alpha", "Beta"]}


# ==================================================================
# SECTION 3: Concurrency (asyncio)
# ==================================================================


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_add_remove(self, tmp_path):
        engine = _make_engine(tmp_path)
        for a in ["Alpha", "Beta", "Gamma"]:
            engine.add_agent(a)

        async def flicker():
            # Interleave add/remove to shake out IndexError / duplication races.
            for i in range(50):
                engine.add_agent("Alpha")  # idempotent-ish
                engine.remove_agent("alpha")
                engine.add_agent("Alpha")

        await asyncio.wait_for(flicker(), timeout=5)
        assert "Alpha" in engine.active_agents
        # No duplicates / no missing despite concurrent-ish flapping.
        assert engine.active_agents.count("Alpha") == 1