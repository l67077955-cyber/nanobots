"""Phase 0: Behavior snapshot tests for the 4-stage compression pipeline.

Each test captures current behavior BEFORE refactoring.
Purpose: detect regressions when logic is moved into pipeline.py + stages/.
"""

import json
import re
import pytest
from unittest.mock import AsyncMock, patch


def mock_provider():
    """A fake LLM provider with chat_with_retry returning a canned summary."""
    provider = AsyncMock()
    class FakeResponse:
        content = "FAKE_SUMMARY: input was truncated"
    provider.chat_with_retry = AsyncMock(return_value=FakeResponse())
    return provider


# ═══════════════════════════════════════════════════
# Stage 1: Tool Result Truncation (result_processor.py)
# ═══════════════════════════════════════════════════

class TestStage1_ToolTruncation:
    """Snapshot: process_tool_result behavior."""

    @pytest.fixture(autouse=True)
    def patch_deps(self):
        with (
            patch("nanobot.groupchat.history.result_processor._get_max_chars") as mock_get,
            patch("nanobot.groupchat.history.result_processor._persist_to_disk") as mock_persist,
        ):
            def _side_effect(tool_name):
                m = {"exec": 10_000, "web_fetch": 8_000, "web_search": 5_000, "read_file": 5_000}
                return m.get(tool_name, 64_000)
            mock_get.side_effect = _side_effect
            mock_persist.return_value = None
            yield

    @pytest.mark.asyncio
    async def test_exec_head_tail_truncation(self):
        """exec uses head_tail strategy: keeps head + tail when over limit."""
        from nanobot.groupchat.history.result_processor import process_tool_result
        output = "line 0: hello world\n" * 500 + "middle\n" * 250 + "tail: end\n" * 500
        assert len(output) > 10_000
        result = await process_tool_result(output, "exec", "call_exec_1")
        assert isinstance(result, str)
        assert len(result) < len(output)
        # AI summarize triggers for >8000c input; result is a summary, not head/tail
        assert "nano:exec" in result  # AI compression marker

    @pytest.mark.asyncio
    async def test_small_result_passthrough(self):
        """Small results under the per-tool limit pass through unchanged."""
        from nanobot.groupchat.history.result_processor import process_tool_result
        small = "hello world"
        # Small enough, no truncation needed
        result = await process_tool_result(small, "exec", "call_exec_2")
        assert result == small

    @pytest.mark.asyncio
    async def test_multimodal_list_passthrough(self):
        """Multimodal lists (with image_url) are returned unchanged (not strings)."""
        from nanobot.groupchat.history.result_processor import process_tool_result
        ml = [{"type": "image_url", "image_url": {"url": "data:..."}}]
        result = await process_tool_result(ml, "exec", "call_exec_3")
        assert result is ml  # same object, unchanged


# ═══════════════════════════════════════════════════
# Stage 3: Context Pruning (tool_pruning.py)
# ═══════════════════════════════════════════════════

class TestStage3_ContextPrune:
    """Snapshot: prune_messages behavior (soft + hard cap)."""

    TOOL_RESULT_5K = "x" * 5000
    TOOL_RESULT_200 = "x" * 200

    def _make_messages(self, n_tool_msgs, recent_assistant_turns=3):
        """Build conversation with system + alternating assistant/tool messages."""
        msgs = [{"role": "system", "content": "You are a helpful assistant."}]
        for i in range(n_tool_msgs):
            msgs.append({"role": "assistant", "content": f"Let me check that step {i}."})
            msgs.append({
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": self.TOOL_RESULT_5K if i < n_tool_msgs - recent_assistant_turns else self.TOOL_RESULT_200,
            })
        return msgs

    def test_soft_pruning_not_triggered(self):
        """When ratio < soft_ratio (now 0.55 by default), no messages are pruned.
        The test uses explicit args. 6*5k chars on 40k-token window is still low ratio.
        """
        msgs = self._make_messages(6)
        from nanobot.groupchat.history.tool_pruning import prune_messages
        result = prune_messages(msgs, context_window_tokens=40_000, soft_ratio=0.3, keep_recent=3)
        assert len(result) == len(msgs)
        tool_contents = [m["content"] for m in result if m.get("role") == "tool"]
        assert any(len(c) == 5000 for c in tool_contents)

    def test_soft_pruning_old_results_replaced(self):
        """When ratio >= 0.3, old tool results get 1-line summary.
        Use small window to force high ratio under accurate tiktoken estimator
        (repetitive 'x' data is very cheap in real tokens).
        """
        from nanobot.groupchat.history.tool_pruning import prune_messages
        msgs = self._make_messages(10)
        # Small window forces the soft trigger even with real token estimates
        result = prune_messages(msgs, context_window_tokens=3000, soft_ratio=0.3, keep_recent=3)
        tool_contents = [m["content"] for m in result if m.get("role") == "tool"]
        short_ones = [c for c in tool_contents if len(c) < 100]
        long_ones = [c for c in tool_contents if len(c) == 200]
        assert len(short_ones) > 0, "Old tool results should be summarized"
        assert len(long_ones) >= 3, "Last 3 protected by keep_recent"

    

    def test_summarize_tool_result_format(self):
        """_summarize_tool_result produces expected 1-line summaries per tool type."""
        from nanobot.groupchat.history.tool_pruning import _summarize_tool_result

        # exec: content must contain "exit_code: N" or "returncode = N" etc for regex to match
        exec_content = "line1\nreturncode: 0\n47 lines in total\n"

        cases = [
            ("exec", '{"command": "npm test"}', exec_content,
             "[exec] ran `npm test` -> exit 0, 4 lines | head:"),  # now includes head preview (improved behavior)
            ("read_file", '{"path": "config.py"}', "file content here",
             "[read_file] read config.py ("),
            ("web_search", '{"query": "python async"}', "search results",
             "[web_search] query='python async'"),
            ("web_fetch", '{"url": "https://example.com"}', "fetched content",
             "[web_fetch] fetched https://example.com"),
            ("list_dir", '{"path": "/tmp"}', "dir listing",
             "[list_dir] scanned /tmp"),
        ]
        for tool_name, args, content, expected_prefix in cases:
            result = _summarize_tool_result(tool_name, args, content)
            assert result.startswith(expected_prefix), f"{tool_name}: expected prefix {expected_prefix!r}, got {result!r}"


# ═══════════════════════════════════════════════════
# Stage 4: History Compression (tool_pruning.py)
# ═══════════════════════════════════════════════════

class TestStage4_HistoryCompress:
    """Snapshot: prune_conversation_tail_with_summary behavior."""

    def _make_long_conversation(self, n_msgs):
        msgs = [{"role": "system", "content": "system"}]
        for i in range(n_msgs):
            msgs.append({"role": "user", "content": f"user msg {i}"})
            msgs.append({"role": "assistant", "content": f"assistant msg {i}"})
        return msgs

    @pytest.mark.asyncio
    async def test_min_dropped_silent_discard(self):
        """When dropped < min_dropped_for_summary, no LLM call, silently discard."""
        from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary
        # 6 pairs = 12 conv msgs. With keep_turns=3, max_conv=9, dropped=3 <5 -> silent
        msgs = self._make_long_conversation(6)
        sys_msg_count = 1
        dropped = await prune_conversation_tail_with_summary(
            msgs, sys_msg_count, keep_turns=3, provider=None, min_dropped_for_summary=5
        )
        # dropped=3 <5, silent discard, kept = sys + 9 (keep_turns*3)
        assert dropped > 0
        assert len(msgs) == sys_msg_count + 9

    @pytest.mark.asyncio
    async def test_summary_injected_when_above_threshold(self):
        """When dropped >= min_dropped_for_summary, AI summary is injected."""
        provider = mock_provider()
        from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary
        msgs = self._make_long_conversation(20)  # 20 pairs, 40 conv msgs
        sys_msg_count = 1
        dropped = await prune_conversation_tail_with_summary(
            msgs, sys_msg_count, keep_turns=3, provider=provider, model="test-model",
            agent_name="test_agent", min_dropped_for_summary=5
        )
        # 20 pairs (40 msgs), keep 6, drop 34. 34 >= 5, summary injected
        assert dropped > 10
        assert provider.chat_with_retry.called

    @pytest.mark.asyncio
    async def test_summary_separator_replacement(self):
        """[上下文摘要] separator prevents accumulation on repeated calls."""
        provider = mock_provider()
        from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary
        msgs = self._make_long_conversation(30)
        sys_msg_count = 1
        await prune_conversation_tail_with_summary(
            msgs, sys_msg_count, keep_turns=3, provider=provider, model="test-model",
            agent_name="test_agent", min_dropped_for_summary=3
        )
        after_first = [m for m in msgs if m.get("role") == "system"]
        last_sys = after_first[-1]["content"]
        assert "[上下文摘要" in last_sys,             f"Expected separator in system message, got: {last_sys[:200]}..."
