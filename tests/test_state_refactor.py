"""Comprehensive tests for the pure variable-driven state.yaml refactoring.

Covers:
    1. Pydantic models (state_models.py) — schema validation, edge cases
    2. FileStateBus (state_bus.py) — init, read/write, poll_changes, agent lifecycle
    3. Persistence (persistence.py) — no more session.jsonl
    4. Broadcast coordinator changes — _handle_change logic
    5. Edge cases — corrupt YAML, empty state, concurrent writes, large data
"""

import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Test setup ──────────────────────────────────────────────────


@pytest.fixture
def tmp_session_dir(tmp_path):
    """Create a temporary session directory."""
    d = tmp_path / "gc-test-session"
    d.mkdir()
    return d


@pytest.fixture
def state_bus(tmp_session_dir):
    """Create a FileStateBus with a temp directory."""
    from nanobot.groupchat.state_bus import FileStateBus
    return FileStateBus(tmp_session_dir)


# ══════════════════════════════════════════════════════════════════
# 1. Pydantic Models (state_models.py)
# ══════════════════════════════════════════════════════════════════


class TestStateModels:
    """Test Pydantic model validation and schema integrity."""

    def test_agent_block_defaults(self):
        """AgentBlock should have sensible defaults."""
        from nanobot.groupchat.state_models import AgentBlock
        agent = AgentBlock()
        assert agent.state == "running"
        assert agent.reply_to == "All"
        assert agent.context_exclude == []
        assert agent.muted is False
        assert agent.activity == "idle"
        assert agent.current_tool is None
        assert agent.cycle == 0
        assert agent.content_preview == ""
        assert agent.toolchain == []
        assert agent.inbox == []
        assert agent.outbox == []

    def test_agent_block_control_variables(self):
        """Control variables should accept valid values."""
        from nanobot.groupchat.state_models import AgentBlock
        agent = AgentBlock(
            state="paused",
            reply_to="Kirk",
            context_exclude=[1, 3, 5],
            muted=True,
        )
        assert agent.state == "paused"
        assert agent.reply_to == "Kirk"
        assert agent.context_exclude == [1, 3, 5]
        assert agent.muted is True

    def test_agent_block_invalid_state_rejected(self):
        """Invalid state values should be rejected by Pydantic."""
        from nanobot.groupchat.state_models import AgentBlock
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentBlock(state="deleted")  # Not in Literal["running", "paused"]

    def test_agent_block_invalid_activity_rejected(self):
        """Invalid activity values should be rejected."""
        from nanobot.groupchat.state_models import AgentBlock
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AgentBlock(activity="sleeping")

    def test_agent_block_null_reply_to(self):
        """reply_to=None should be valid (agent thinks but doesn't reply)."""
        from nanobot.groupchat.state_models import AgentBlock
        agent = AgentBlock(reply_to=None)
        assert agent.reply_to is None

    def test_session_meta_minimal(self):
        """SessionMeta should work with just id."""
        from nanobot.groupchat.state_models import SessionMeta
        s = SessionMeta(id="gc-test")
        assert s.id == "gc-test"
        assert s.leader is None
        assert s.topic == ""
        assert s.round == 0

    def test_conversation_entry(self):
        """ConversationEntry should store all fields."""
        from nanobot.groupchat.state_models import ConversationEntry
        e = ConversationEntry(seq=1, sender="用户", content="hello", ts="2026-04-03T00:00:00")
        assert e.seq == 1
        assert e.sender == "用户"

    def test_full_state_data_roundtrip(self):
        """GroupChatStateData should serialize and deserialize cleanly."""
        from nanobot.groupchat.state_models import GroupChatStateData, AgentBlock, SessionMeta
        data = GroupChatStateData(
            session=SessionMeta(id="gc-test", leader="Kirk", topic="test", round=1),
            agents={
                "Kirk": AgentBlock(state="running", activity="thinking"),
                "Harper": AgentBlock(state="paused", muted=True, context_exclude=[2]),
            },
            conversation=[],
            leader_data={"key": "value"},
        )
        dumped = data.model_dump(by_alias=True)
        # Roundtrip
        restored = GroupChatStateData.model_validate(dumped)
        assert restored.agents["Kirk"].state == "running"
        assert restored.agents["Harper"].muted is True
        assert restored.agents["Harper"].context_exclude == [2]
        assert restored.leader_data["key"] == "value"

    def test_inbox_message_alias(self):
        """InboxMessage 'from' alias should work."""
        from nanobot.groupchat.state_models import InboxMessage
        msg = InboxMessage(**{"from": "Kirk", "content": "hello", "ts": "now"})
        assert msg.from_agent == "Kirk"
        dumped = msg.model_dump(by_alias=True)
        assert "from" in dumped
        assert dumped["from"] == "Kirk"

    def test_toolchain_entry(self):
        """ToolChainEntry should handle all optional fields."""
        from nanobot.groupchat.state_models import ToolChainEntry
        entry = ToolChainEntry(tool="web_search", args={"query": "test"}, started="now")
        assert entry.finished is None
        assert entry.ok is None
        entry2 = ToolChainEntry(
            tool="exec", args={}, started="now",
            finished="later", ok=True, len=1000, preview="output..."
        )
        assert entry2.ok is True
        assert entry2.len == 1000

    def test_no_control_section(self):
        """GroupChatStateData should NOT have a control section."""
        from nanobot.groupchat.state_models import GroupChatStateData
        data = GroupChatStateData()
        dumped = data.model_dump()
        assert "control" not in dumped
        assert "commands" not in dumped


# ══════════════════════════════════════════════════════════════════
# 2. FileStateBus (state_bus.py)
# ══════════════════════════════════════════════════════════════════


class TestFileStateBus:
    """Test FileStateBus operations."""

    def test_init_creates_directory(self, tmp_path):
        """FileStateBus should create the session directory."""
        from nanobot.groupchat.state_bus import FileStateBus
        d = tmp_path / "new-session"
        bus = FileStateBus(d)
        assert d.exists()

    def test_init_session(self, state_bus, tmp_session_dir):
        """init_session should create state.yaml with correct structure."""
        state_bus.init_session(
            leader="Kirk",
            topic="test topic",
            round_num=1,
            active_agents=["Kirk", "Harper"],
        )
        assert (tmp_session_dir / "state.yaml").exists()
        snap = state_bus.snapshot()
        assert snap["session"]["id"] == "gc-test-session"
        assert snap["session"]["leader"] == "Kirk"
        assert snap["session"]["topic"] == "test topic"
        assert "Kirk" in snap["agents"]
        assert "Harper" in snap["agents"]
        assert snap["agents"]["Kirk"]["state"] == "running"

    def test_no_session_jsonl_created(self, state_bus, tmp_session_dir):
        """session.jsonl should NOT be created — we removed it."""
        state_bus.init_session(
            leader="Kirk", topic="test", round_num=1, active_agents=["Kirk"],
        )
        assert not (tmp_session_dir / "session.jsonl").exists()

    def test_set_agent_activity(self, state_bus):
        """set_agent_activity should update status variables."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.set_agent_activity("Kirk", "tool_calling", current_tool="web_search", cycle=2)
        snap = state_bus.snapshot()
        assert snap["agents"]["Kirk"]["activity"] == "tool_calling"
        assert snap["agents"]["Kirk"]["current_tool"] == "web_search"
        assert snap["agents"]["Kirk"]["cycle"] == 2

    def test_set_agent_activity_deleted_agent_noop(self, state_bus):
        """set_agent_activity on a deleted agent should be a no-op."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        # Should not raise or create a phantom agent block
        state_bus.set_agent_activity("NonExistent", "thinking")
        snap = state_bus.snapshot()
        assert "NonExistent" not in snap["agents"]

    def test_update_agent(self, state_bus):
        """update_agent should partially update fields."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.update_agent("Kirk", content_preview="Hello world...")
        snap = state_bus.snapshot()
        assert snap["agents"]["Kirk"]["content_preview"] == "Hello world..."

    def test_append_tool_start(self, state_bus):
        """append_tool_start should add to toolchain and update activity."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.append_tool_start("Kirk", "web_search", {"query": "test"})
        snap = state_bus.snapshot()
        assert snap["agents"]["Kirk"]["activity"] == "tool_calling"
        assert snap["agents"]["Kirk"]["current_tool"] == "web_search"
        assert len(snap["agents"]["Kirk"]["toolchain"]) == 1
        assert snap["agents"]["Kirk"]["toolchain"][0]["tool"] == "web_search"

    def test_complete_tool(self, state_bus):
        """complete_tool should update toolchain and reset activity."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.append_tool_start("Kirk", "exec", {"command": "ls"})
        state_bus.complete_tool("Kirk", "exec", 100, True, "file1\nfile2")
        snap = state_bus.snapshot()
        assert snap["agents"]["Kirk"]["activity"] == "thinking"
        assert snap["agents"]["Kirk"]["current_tool"] is None
        tc = snap["agents"]["Kirk"]["toolchain"][0]
        assert tc["ok"] is True
        assert tc["len"] == 100
        assert tc["finished"] is not None

    def test_deliver_message(self, state_bus):
        """deliver_message should update outbox and inbox."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk", "Harper"],
        )
        state_bus.deliver_message("Kirk", ["Harper"], "Do the search", all_agents=["Kirk", "Harper"])
        snap = state_bus.snapshot()
        assert len(snap["agents"]["Kirk"]["outbox"]) == 1
        assert snap["agents"]["Kirk"]["outbox"][0]["to"] == ["Harper"]
        assert len(snap["agents"]["Harper"]["inbox"]) == 1
        assert snap["agents"]["Harper"]["inbox"][0]["from"] == "Kirk"

    def test_deliver_message_all(self, state_bus):
        """deliver_message to 'All' should expand to all agents except sender."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk", "Harper", "Verifier"],
        )
        state_bus.deliver_message("Kirk", ["All"], "Broadcast!", all_agents=["Kirk", "Harper", "Verifier"])
        snap = state_bus.snapshot()
        assert len(snap["agents"]["Harper"]["inbox"]) == 1
        assert len(snap["agents"]["Verifier"]["inbox"]) == 1
        # Kirk shouldn't receive his own message
        assert len(snap["agents"]["Kirk"]["inbox"]) == 0

    def test_append_conversation(self, state_bus):
        """append_conversation should add sequenced entries."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.append_conversation("用户", "Hello")
        state_bus.append_conversation("Kirk", "Hi there!")
        snap = state_bus.snapshot()
        assert len(snap["conversation"]) == 2
        assert snap["conversation"][0]["seq"] == 1
        assert snap["conversation"][1]["seq"] == 2
        assert snap["conversation"][0]["sender"] == "用户"

    def test_rewrite_conversation(self, state_bus):
        """rewrite_conversation should replace the entire chain."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.append_conversation("用户", "msg1")
        state_bus.append_conversation("Kirk", "msg2")
        state_bus.append_conversation("用户", "msg3")
        # Rewrite to shorter
        state_bus.rewrite_conversation([
            {"sender": "用户", "content": "simplified"},
        ])
        snap = state_bus.snapshot()
        assert len(snap["conversation"]) == 1
        assert snap["conversation"][0]["content"] == "simplified"

    def test_conversation_content_truncation(self, state_bus):
        """Long conversation content should be truncated."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        long_content = "x" * 5000
        state_bus.append_conversation("Kirk", long_content)
        snap = state_bus.snapshot()
        assert len(snap["conversation"][0]["content"]) == 1000  # Truncated

    def test_update_session(self, state_bus):
        """update_session should modify session fields."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.update_session(round=2, topic="updated topic")
        snap = state_bus.snapshot()
        assert snap["session"]["round"] == 2
        assert snap["session"]["topic"] == "updated topic"

    def test_get_agent_control(self, state_bus):
        """get_agent_control should return control variables."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        ctrl = state_bus.get_agent_control("Kirk")
        assert ctrl["state"] == "running"
        assert ctrl["reply_to"] == "All"
        assert ctrl["context_exclude"] == []
        assert ctrl["muted"] is False

    def test_get_agent_control_nonexistent(self, state_bus):
        """get_agent_control for missing agent should return defaults."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        ctrl = state_bus.get_agent_control("Ghost")
        assert ctrl["state"] == "running"  # Default


    # ── poll_changes tests ──────────────────────────────────

    def test_poll_changes_no_change(self, state_bus):
        """poll_changes with no changes should return empty list."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        changes = state_bus.poll_changes()
        assert changes == []

    def test_poll_changes_agent_added(self, state_bus, tmp_session_dir):
        """poll_changes should detect new agent blocks."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        # Simulate leader adding an agent block via edit_file
        snap = state_bus.snapshot()
        snap["agents"]["Harper"] = {
            "state": "running",
            "reply_to": "All",
            "context_exclude": [],
            "muted": False,
            "activity": "idle",
            "current_tool": None,
            "cycle": 0,
            "content_preview": "",
            "toolchain": [],
            "inbox": [],
            "outbox": [],
        }
        state_bus._write_all(snap)

        changes = state_bus.poll_changes()
        added = [c for c in changes if c["type"] == "agent_added"]
        assert len(added) == 1
        assert added[0]["name"] == "Harper"

    def test_poll_changes_agent_removed(self, state_bus):
        """poll_changes should detect removed agent blocks."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk", "Harper"],
        )
        # Simulate leader deleting Harper's block
        snap = state_bus.snapshot()
        del snap["agents"]["Harper"]
        state_bus._write_all(snap)

        changes = state_bus.poll_changes()
        removed = [c for c in changes if c["type"] == "agent_removed"]
        assert len(removed) == 1
        assert removed[0]["name"] == "Harper"

    def test_poll_changes_state_changed(self, state_bus):
        """poll_changes should detect state variable changes."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk", "Harper"],
        )
        # Simulate leader pausing Harper
        snap = state_bus.snapshot()
        snap["agents"]["Harper"]["state"] = "paused"
        state_bus._write_all(snap)

        changes = state_bus.poll_changes()
        state_changes = [c for c in changes if c["type"] == "state_changed"]
        assert len(state_changes) == 1
        assert state_changes[0]["name"] == "Harper"
        assert state_changes[0]["old"] == "running"
        assert state_changes[0]["new"] == "paused"

    def test_poll_changes_muted_changed(self, state_bus):
        """poll_changes should detect mute changes."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        snap = state_bus.snapshot()
        snap["agents"]["Kirk"]["muted"] = True
        state_bus._write_all(snap)

        changes = state_bus.poll_changes()
        muted_changes = [c for c in changes if c["type"] == "muted_changed"]
        assert len(muted_changes) == 1
        assert muted_changes[0]["muted"] is True

    def test_poll_changes_conversation_rewritten(self, state_bus):
        """poll_changes should detect conversation rewrites (shortened)."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.append_conversation("用户", "msg1")
        state_bus.append_conversation("Kirk", "msg2")
        state_bus.append_conversation("用户", "msg3")
        # Take snapshot
        _ = state_bus.poll_changes()

        # Now rewrite to shorter
        state_bus.rewrite_conversation([{"sender": "用户", "content": "only one"}])
        changes = state_bus.poll_changes()
        conv_changes = [c for c in changes if c["type"] == "conversation_rewritten"]
        assert len(conv_changes) == 1

    def test_poll_changes_multiple_changes(self, state_bus):
        """poll_changes should detect multiple simultaneous changes."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk", "Harper", "Verifier"],
        )
        snap = state_bus.snapshot()
        # Simultaneously: pause Harper, mute Verifier, add new agent
        snap["agents"]["Harper"]["state"] = "paused"
        snap["agents"]["Verifier"]["muted"] = True
        snap["agents"]["NewAgent"] = {
            "state": "running", "reply_to": "All", "context_exclude": [],
            "muted": False, "activity": "idle", "current_tool": None,
            "cycle": 0, "content_preview": "", "toolchain": [], "inbox": [], "outbox": [],
        }
        state_bus._write_all(snap)

        changes = state_bus.poll_changes()
        types = [c["type"] for c in changes]
        assert "agent_added" in types
        assert "state_changed" in types
        assert "muted_changed" in types

    def test_poll_changes_idempotent(self, state_bus):
        """Calling poll_changes twice without changes should return empty."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        snap = state_bus.snapshot()
        snap["agents"]["Kirk"]["state"] = "paused"
        state_bus._write_all(snap)

        changes1 = state_bus.poll_changes()
        assert len(changes1) > 0

        changes2 = state_bus.poll_changes()
        assert changes2 == []  # No further changes

    def test_preserve_conversation_across_rounds(self, state_bus):
        """init_session should preserve conversation when re-initing."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state_bus.append_conversation("用户", "round1 message")

        # Re-init for round 2
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=2, active_agents=["Kirk"],
        )
        snap = state_bus.snapshot()
        assert len(snap["conversation"]) == 1
        assert snap["conversation"][0]["content"] == "round1 message"
        assert snap["session"]["round"] == 2

    def test_preserve_leader_data_across_rounds(self, state_bus):
        """init_session should preserve leader_data when re-initing."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        # Directly write leader_data
        snap = state_bus.snapshot()
        snap["leader_data"]["task_plan"] = "Step 1: Search"
        state_bus._write_all(snap)

        # Re-init for round 2
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=2, active_agents=["Kirk"],
        )
        snap = state_bus.snapshot()
        assert snap["leader_data"]["task_plan"] == "Step 1: Search"


# ══════════════════════════════════════════════════════════════════
# 3. Edge Cases & Robustness
# ══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Test edge cases and robustness."""

    def test_empty_state_yaml(self, tmp_session_dir):
        """FileStateBus should handle an empty/missing state.yaml."""
        from nanobot.groupchat.state_bus import FileStateBus
        bus = FileStateBus(tmp_session_dir)
        snap = bus.snapshot()
        assert isinstance(snap, dict)

    def test_corrupt_yaml_recovery(self, tmp_session_dir):
        """FileStateBus should handle corrupt YAML gracefully."""
        from nanobot.groupchat.state_bus import FileStateBus
        # Write corrupt YAML
        (tmp_session_dir / "state.yaml").write_text("{{invalid: yaml: [broken")
        bus = FileStateBus(tmp_session_dir)
        # Should not crash — returns best-effort dict
        snap = bus.snapshot()
        assert isinstance(snap, dict)

    def test_large_toolchain(self, state_bus):
        """State bus should handle large toolchains without issues."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        for i in range(50):
            state_bus.append_tool_start("Kirk", f"tool_{i}", {"arg": f"val_{i}"})
            state_bus.complete_tool("Kirk", f"tool_{i}", 100, True)
        snap = state_bus.snapshot()
        assert len(snap["agents"]["Kirk"]["toolchain"]) == 50

    def test_long_tool_args_truncated(self, state_bus):
        """Tool args with long strings should be truncated."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        long_arg = "x" * 1000
        state_bus.append_tool_start("Kirk", "exec", {"command": long_arg})
        snap = state_bus.snapshot()
        tc = snap["agents"]["Kirk"]["toolchain"][0]
        assert len(tc["args"]["command"]) <= 203  # 200 + "..."

    def test_many_agents(self, state_bus):
        """State bus should handle many agents."""
        agents = [f"Agent_{i}" for i in range(20)]
        state_bus.init_session(
            leader="Agent_0", topic="t", round_num=1, active_agents=agents,
        )
        snap = state_bus.snapshot()
        assert len(snap["agents"]) == 20

    def test_thread_safety_basic(self, state_bus):
        """Basic thread safety: concurrent writes should not corrupt state."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        errors = []

        def writer(i):
            try:
                state_bus.append_conversation(f"Agent_{i}", f"Message {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        snap = state_bus.snapshot()
        assert len(snap["conversation"]) == 10

    def test_seq_recovery_after_restart(self, tmp_session_dir):
        """After restart, seq should continue from where it left off."""
        from nanobot.groupchat.state_bus import FileStateBus
        bus1 = FileStateBus(tmp_session_dir)
        bus1.init_session(leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"])
        bus1.append_conversation("用户", "msg1")
        bus1.append_conversation("Kirk", "msg2")

        # Simulate restart
        bus2 = FileStateBus(tmp_session_dir)
        bus2.append_conversation("用户", "msg3")
        snap = bus2.snapshot()
        seqs = [m["seq"] for m in snap["conversation"]]
        assert seqs == [1, 2, 3]  # No duplicate seq

    def test_deliver_to_deleted_agent(self, state_bus):
        """Delivering a message to a deleted agent should not crash."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        # Deliver to non-existent agent — should be a no-op, not crash
        state_bus.deliver_message("Kirk", ["Ghost"], "hello", all_agents=["Kirk"])
        snap = state_bus.snapshot()
        # Kirk's outbox should still have the message
        assert len(snap["agents"]["Kirk"]["outbox"]) == 1

    def test_poll_changes_after_complete_removal_and_readd(self, state_bus):
        """Agent removed then re-added should show up in poll_changes."""
        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk", "Harper"],
        )
        # Remove Harper
        snap = state_bus.snapshot()
        del snap["agents"]["Harper"]
        state_bus._write_all(snap)
        changes = state_bus.poll_changes()
        assert any(c["type"] == "agent_removed" and c["name"] == "Harper" for c in changes)

        # Re-add Harper
        snap = state_bus.snapshot()
        snap["agents"]["Harper"] = state_bus._empty_agent()
        state_bus._write_all(snap)
        changes = state_bus.poll_changes()
        assert any(c["type"] == "agent_added" and c["name"] == "Harper" for c in changes)


# ══════════════════════════════════════════════════════════════════
# 4. Persistence Layer
# ══════════════════════════════════════════════════════════════════


class TestPersistence:
    """Test that persistence.py no longer writes session.jsonl."""

    def test_save_message_syncs_to_state_bus(self, state_bus):
        """save_message should sync to state_bus.append_conversation."""
        from nanobot.groupchat.persistence import GroupChatState
        registry = {"Kirk": {"model": "test"}}
        state = GroupChatState(registry)
        state.state_bus = state_bus

        state_bus.init_session(
            leader="Kirk", topic="t", round_num=1, active_agents=["Kirk"],
        )
        state.save_message("用户", "hello", [{"sender": "用户", "content": "hello"}])

        snap = state_bus.snapshot()
        assert len(snap["conversation"]) == 1
        assert snap["conversation"][0]["sender"] == "用户"

    def test_save_message_no_state_bus_noop(self):
        """save_message with no state_bus should not crash."""
        from nanobot.groupchat.persistence import GroupChatState
        registry = {"Kirk": {"model": "test"}}
        state = GroupChatState(registry)
        # state.state_bus is None by default
        state.save_message("用户", "hello", [])  # Should not crash

    def test_no_save_event_method(self):
        """GroupChatState should NOT have save_event method anymore."""
        from nanobot.groupchat.persistence import GroupChatState
        registry = {}
        state = GroupChatState(registry)
        assert not hasattr(state, 'save_event')

    def test_no_save_round_summary_method(self):
        """GroupChatState should NOT have save_round_summary method anymore."""
        from nanobot.groupchat.persistence import GroupChatState
        registry = {}
        state = GroupChatState(registry)
        assert not hasattr(state, 'save_round_summary')


# ══════════════════════════════════════════════════════════════════
# 5. Broadcast Coordinator
# ══════════════════════════════════════════════════════════════════


class TestBroadcastChanges:
    """Test BroadcastCoordinator._handle_change logic."""

    @pytest.fixture
    def mock_engine(self):
        engine = MagicMock()
        engine._leader = "Kirk"
        engine._active_agents = ["Kirk", "Harper"]
        engine._send = AsyncMock()
        engine._history = []
        engine._muted_agents = set()
        engine.registry = {
            "Kirk": {"model": "test-model", "prompt": "test"},
            "Harper": {"model": "test-model", "prompt": "test"},
        }
        engine.mute_agent = MagicMock()
        engine.unmute_agent = MagicMock()
        engine.remove_agent = MagicMock(return_value="removed")
        return engine

    @pytest.mark.asyncio
    async def test_handle_muted_change(self, mock_engine):
        """_handle_change for muted_changed should call engine.mute_agent."""
        from nanobot.groupchat.broadcast import BroadcastCoordinator
        coord = BroadcastCoordinator(["Kirk", "Harper"], mock_engine, MagicMock())
        coord.state_bus = MagicMock()

        await coord._handle_change({
            "type": "muted_changed",
            "name": "Harper",
            "muted": True,
        })
        mock_engine.mute_agent.assert_called_once_with("Harper")

    @pytest.mark.asyncio
    async def test_handle_unmuted_change(self, mock_engine):
        """_handle_change for unmuting should call engine.unmute_agent."""
        from nanobot.groupchat.broadcast import BroadcastCoordinator
        coord = BroadcastCoordinator(["Kirk", "Harper"], mock_engine, MagicMock())
        coord.state_bus = MagicMock()

        await coord._handle_change({
            "type": "muted_changed",
            "name": "Harper",
            "muted": False,
        })
        mock_engine.unmute_agent.assert_called_once_with("Harper")

    @pytest.mark.asyncio
    async def test_handle_agent_removed(self, mock_engine):
        """_handle_change for agent_removed should cancel task and remove agent."""
        from nanobot.groupchat.broadcast import BroadcastCoordinator
        coord = BroadcastCoordinator(["Kirk", "Harper"], mock_engine, MagicMock())
        coord.state_bus = MagicMock()
        coord._agent_tasks = {}

        await coord._handle_change({
            "type": "agent_removed",
            "name": "Harper",
        })
        mock_engine.remove_agent.assert_called_once_with("Harper")

    @pytest.mark.asyncio
    async def test_handle_state_paused(self, mock_engine):
        """_handle_change for state_changed to paused should cancel the agent."""
        from nanobot.groupchat.broadcast import BroadcastCoordinator
        coord = BroadcastCoordinator(["Kirk", "Harper"], mock_engine, MagicMock())
        coord.state_bus = MagicMock()

        # Create a mock running task
        mock_task = MagicMock()
        mock_task.done.return_value = False
        coord._agent_tasks = {mock_task: "Harper"}

        await coord._handle_change({
            "type": "state_changed",
            "name": "Harper",
            "old": "running",
            "new": "paused",
        })
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_conversation_rewritten(self, mock_engine):
        """_handle_change for conversation_rewritten should sync to engine._history."""
        from nanobot.groupchat.broadcast import BroadcastCoordinator
        coord = BroadcastCoordinator(["Kirk"], mock_engine, MagicMock())
        coord.state_bus = MagicMock()
        coord.state_bus.snapshot.return_value = {
            "conversation": [
                {"seq": 1, "sender": "用户", "content": "new msg", "ts": "now"},
            ],
        }

        await coord._handle_change({"type": "conversation_rewritten"})
        assert len(mock_engine._history) == 1
        assert mock_engine._history[0]["sender"] == "用户"


# ══════════════════════════════════════════════════════════════════
# 6. Import Verification — no broken imports
# ══════════════════════════════════════════════════════════════════


class TestImports:
    """Verify all modules import cleanly."""

    def test_import_state_models(self):
        from nanobot.groupchat.state_models import GroupChatStateData, AgentBlock, SessionMeta

    def test_import_state_bus(self):
        from nanobot.groupchat.state_bus import FileStateBus

    def test_import_broadcast(self):
        from nanobot.groupchat.broadcast import broadcast_round, BroadcastCoordinator

    def test_import_persistence(self):
        from nanobot.groupchat.persistence import GroupChatState

    def test_import_agent_runner(self):
        from nanobot.groupchat.agent_runner import AgentRunner, AgentResult, AgentState

    def test_no_tasks_module(self):
        """tasks.py should be deleted — import should fail."""
        with pytest.raises(ModuleNotFoundError):
            import nanobot.groupchat.tasks

    def test_no_control_command_in_models(self):
        """ControlCommand should not exist in state_models anymore."""
        import nanobot.groupchat.state_models as sm
        assert not hasattr(sm, 'ControlCommand')
        assert not hasattr(sm, 'ControlSection')
