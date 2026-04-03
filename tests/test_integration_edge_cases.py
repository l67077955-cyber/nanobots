"""Integration tests for extreme edge cases in Memory and Groupchat sync logic.

This module covers:
1. MemoryStore fallback and RAG index limits
2. FileStateBus YAML corruption and user message loss recovery (in BroadcastCoordinator)
3. StreamingDisplay sequence jitter and ID abandonment on tool reset
4. AgentRunner/BroadcastCoordinator early stop, duplicates, and timeouts
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.groupchat.state_bus import FileStateBus
from nanobot.groupchat.streaming import StreamingDisplay
from nanobot.groupchat.agent_runner import AgentRunner, AgentState
from nanobot.groupchat.broadcast import BroadcastCoordinator


# ══════════════════════════════════════════════════════════════════
# 1. Advanced Memory Tests 
# ══════════════════════════════════════════════════════════════════

class TestAdvancedMemory:
    """Testing memory frontmatter, indexing limits, and fallback scenarios."""

    def test_memory_indexing_and_limits(self, tmp_path):
        """Index should correctly parse frontmatter and cap at MAX_INDEX_LINES."""
        store = MemoryStore(tmp_path)
        store.MAX_INDEX_LINES = 3  # Force small cap for test
        
        # Write 5 memories 
        for i in range(5):
            store.write_memory(
                f"mem_{i}.md",
                f"Content {i}",
                name=f"Mem {i}",
                description=f"Desc {i}",
                memory_type="user"
            )
            import time
            time.sleep(0.01) # ensure mtime ordering

        results = store.scan_memories()
        assert len(results) == 5
        
        # Newest files should have the highest mtime, so they appear first
        index_text = store.build_memory_index()
        lines = [line for line in index_text.split("\n") if line.strip()]
        
        # Should be capped
        assert len(lines) == 3
        
        # Check formatting
        assert "- [user] [Mem 4](mem_4.md) — Desc 4" in lines[0]
        
    @pytest.mark.asyncio
    async def test_tool_fallback_behavior_on_memory_consolidation(self, tmp_path):
        """If the LLM provider fails tool_choice="forced" or provides empty payload, we must not crash."""
        store = MemoryStore(tmp_path)
        
        mock_provider = AsyncMock()
        mock_model = "fake-model"
        
        # 1. Simulate provider returning `finish_reason="error"` due to missing tool support, then falling back
        # The fallback auto-tool call will give empty arguments, leading to an archive.
        class FakeResp1:
            finish_reason = "error"
            content = "tool_choice 'forced' is not supported"
            has_tool_calls = False
        
        class FakeResp2:
            finish_reason = "stop"
            content = "OK I will do it"
            has_tool_calls = False
            
        mock_provider.chat_with_retry.side_effect = [FakeResp1(), FakeResp2()]
        
        messages = [{"role": "user", "content": "hello"}]
        res = await store.consolidate(messages, mock_provider, mock_model)
        
        # Should return correctly (fails internal threshold, but doesn't throw)
        assert res is False
        assert store._consecutive_failures == 1
        
        # 2. Re-trigger until raw archive happens
        for _ in range(store._MAX_FAILURES_BEFORE_RAW_ARCHIVE - 1):
            mock_provider.chat_with_retry.side_effect = [FakeResp1(), FakeResp2()]
            res = await store.consolidate(messages, mock_provider, mock_model)
            
        # The last one triggers raw archive and returns True
        assert res is True
        assert "RAW" in store.read_long_term() or "RAW" in (store.history_file.read_text() if store.history_file.exists() else "")
        assert store._consecutive_failures == 0


# ══════════════════════════════════════════════════════════════════
# 2. State Inconsistency & Recovery
# ══════════════════════════════════════════════════════════════════

class TestStateInconsistency:

    def test_recover_corrupt_yaml(self, tmp_path):
        """If state.yaml gets completely mangled by concurrent write or leader mistake, we survive."""
        d = tmp_path / "gc-session"
        bus = FileStateBus(d)
        
        # Inject corrupted YAML mid-flight
        bus._file.write_text("session: { id: gc-session \ngarbage:::::", encoding="utf-8")
        
        # Should gracefully return {} or basic dict matching model empty output
        snap = bus.snapshot()
        assert isinstance(snap, dict)
        assert snap.get("session") is None

    @pytest.mark.asyncio
    async def test_user_message_recovery(self):
        """Simulation of conversation_rewritten logic in BroadcastCoordinator ensuring user messages aren't lost."""
        from nanobot.groupchat.broadcast import BroadcastCoordinator
        
        # Mock Engine
        mock_engine = MagicMock()
        mock_engine._history = [
            {"sender": "系统", "content": "START"},
            {"sender": "Kirk", "content": "Task 1"},
            {"sender": "用户", "content": "Important user instruction!"}, # Missing from yaml
        ]
        mock_engine._send = AsyncMock()
        
        coord = BroadcastCoordinator(["Kirk"], mock_engine, MagicMock())
        coord.state_bus = MagicMock()
        
        # Simulating the exact scenario where the leader rewriting state.yaml missed the user's latest message
        coord.state_bus.snapshot.return_value = {
            "conversation": [
                {"sender": "系统", "content": "START"},
                {"sender": "Kirk", "content": "Task 1"},
                # Missing the user message here
            ]
        }
        
        await coord._handle_change({"type": "conversation_rewritten"})
        
        # Let's verify the user message wasn't lost in engine history!
        history_senders = [m["sender"] for m in mock_engine._history]
        history_contents = [m["content"] for m in mock_engine._history]
        
        assert "用户" in history_senders
        assert "Important user instruction!" in history_contents
        assert len(mock_engine._history) == 3


# ══════════════════════════════════════════════════════════════════
# 3. Message Jitter and Streaming Interleaved Tool resets
# ══════════════════════════════════════════════════════════════════

class TestStreamingJitter:
    
    @pytest.mark.asyncio
    async def test_rapid_jitter_and_tool_interruption(self):
        """Simulate rapid interleaved tokens and tool interruptions to test ID logic."""
        
        send_fn = AsyncMock(return_value=999) 
        edit_fn = AsyncMock()
        
        # Setup stream
        stream = StreamingDisplay("👑 Leader ━━", send_fn, edit_fn)
        stream.EDIT_INTERVAL = 0.0 # Force edits immediately for test
        
        # Delta 1: initial send
        await stream.on_delta("Hello, ")
        send_fn.assert_awaited_once()
        assert stream.msg_id == 999
        
        # Tool call interrupts the stream!
        await stream.on_reset()
        # Ensure older msg_id transitioned
        assert stream.msg_id is None
        assert stream._pre_tool_msg_id == 999
        edit_fn.assert_awaited_with(999, "👑 Leader ━━🔧 ...")
        
        # Resume streaming delta after tool finishes
        send_fn.return_value = 888  # New message generated below tool
        await stream.on_delta("World!")
        
        # Expected behavior: A completely new message was sent
        assert send_fn.call_count == 2
        assert stream.msg_id == 888
        
        # Finalization
        await stream.finalize("Hello, World!")
        # 1. Finalize updates the NEW streaming message (888) with final text
        # 2. Finalize caps the OLD streaming message (999) to indicate it's abandoned
        edit_fn.assert_any_call(888, "👑 Leader ━━Hello, World!")
        edit_fn.assert_any_call(999, "👑 Leader ━━↓")


# ══════════════════════════════════════════════════════════════════
# 4. Leader Runtime Extreme Edge Cases
# ══════════════════════════════════════════════════════════════════

class TestLeaderEdgeCases:

    @pytest.mark.asyncio
    async def test_leader_duplicate_output_trap(self):
        """Leader stuck in a loop without tools should be terminated early."""
        # Setup mock engine
        mock_engine = MagicMock()
        mock_engine.config.max_tokens = 100
        mock_engine.registry = {"Kirk": {"model": "test-model"}}
        mock_engine._send = AsyncMock()
        
        # Fake mailbox
        mock_mailbox = MagicMock()
        # Never give message to Leader, simulate nobody responding
        mock_mailbox.wait = AsyncMock(return_value=None) 
        
        # Agent Runner for Leader
        runner = AgentRunner(
            "Kirk", 0, 1,
            engine=mock_engine,
            mailbox=mock_mailbox,
            tool_registry=MagicMock(),
            tool_defs=[],
            messages=[{"role": "user", "content": "hi"}],
            model="fake",
            is_leader=True
        )
        
        # Simulate tool_loop yielding the EXACT same content without tools
        class MockResult:
            content = "This is a duplicated response."
            tools_used = []
            latency = 1.0
            iterations = 1
            finish_reason = "stop"
            tool_calls_detail = []

        with patch("nanobot.agent.tool_loop.tool_loop", new_callable=AsyncMock) as mocked_loop:
            mocked_loop.return_value = MockResult()
            
            # The runner should stop entirely after cycle 2 due to duplication guard
            result = await runner.run()
            
            # Leader can run up to MAX_CYCLES (6), but we expect it to break early
            assert runner.total_iterations == 2 
            assert result.content == "This is a duplicated response."
            assert result.state == AgentState.DONE

    @pytest.mark.asyncio
    async def test_broadcast_coordinator_timeout_leak(self):
        """If agents loop forever or deadlock, BroadcastCoordinator global timeout breaks it cleanly."""
        mock_engine = MagicMock()
        mock_engine._round = 1
        mock_engine._send = AsyncMock()
        
        coord = BroadcastCoordinator(["Kirk"], mock_engine, MagicMock(), global_timeout=0.1)
        coord._user_question = "x"
        coord.state_bus = MagicMock()
        
        # Simulate runner that sleeps infinitely
        async def endless_run():
            await asyncio.sleep(100)
            
        mock_runner = MagicMock()
        mock_runner.run = endless_run
        coord.runners = {"Kirk": mock_runner}
        
        # We start the coord. We expect the overarching `asyncio.wait(timeout=SAFETY_LIMIT)` 
        # or the timeout mechanic internally to cleanly survive, but since `global_timeout` is 
        # mainly for mailbox waiting, to actually break the deadlock, we'd rely on cancellation.
        # We manually simulate the `SAFETY_LIMIT` timeout by mocking wait.
        
        with patch.object(asyncio, "wait", autospec=True) as mock_wait:
            # First call drops due to timeout (returning empty done set)
            mock_wait.return_value = (set(), set())
            
            # Note: This will exit normally with an empty result if tasks get cancelled and finish
            await coord.run()
            
            results = coord.get_results()
            assert len(results) == 0 # no completed task since they were cancelled
