#!/usr/bin/env python3
"""
Test: broadcast.py synthesis display + end-of-discussion handling.

Covers the stable-aligned behavior restored from v0.1.4.post7-stable:
1. Leader end_discussion with no text → exactly ONE forced synthesis cycle
   (no _MIN_SYNTHESIS_LEN / synthesis_quality_check retry loop).
2. Synthesis display uses `进展 [N]` label (leader) and is shown even when
   chatroom_send was used (teammate-targeted sends must not swallow the
   user-facing synthesis).
3. Non-leader wait timeout keeps waiting (no MAX_CONSECUTIVE_WAITS force-exit)
   to avoid cascading stalls.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROADCAST = ROOT / "nanobot/groupchat/runtime/broadcast.py"
AGENT_CYCLE = ROOT / "nanobot/groupchat/runtime/agent_cycle.py"
HISTORY = ROOT / "nanobot/groupchat/context/component_manager.py"
CYCLE_SRC = AGENT_CYCLE  # per-agent body (was nested in broadcast)


# ── Test 1: Syntax Integrity ──────────────────────────────

class TestSyntax(unittest.TestCase):
    """Verify all modified files parse cleanly."""

    def test_broadcast_syntax(self):
        ast.parse(BROADCAST.read_text(encoding='utf-8'))
        ast.parse(AGENT_CYCLE.read_text(encoding='utf-8'))
        self.assertTrue(True, "broadcast + agent_cycle syntax OK")

    def test_component_manager_syntax(self):
        with open(HISTORY) as f:
            ast.parse(f.read())
        self.assertTrue(True, "component_manager.py syntax OK")


# ── Test 2: Synthesis display via streaming finalize ─────────

class TestSynthesisDisplay(unittest.TestCase):
    """Leader synthesis is finalized in place on the streaming message (no
    duplicate send, no _used_chatroom_send guard). The display layer's
    StreamingDisplay owns the ▍ header via agent_header(mode='broadcast')."""

    def test_synthesis_uses_stream_finalize(self):
        content = CYCLE_SRC.read_text(encoding='utf-8')
        idx = content.find("displayed {} synthesis output")
        self.assertGreater(idx, 0, "synthesis display log marker found")
        block = content[max(0, idx - 400):idx]
        self.assertIn("_stream.finalize", block,
            "Synthesis display finalizes the streaming message in place (no duplicate send)")

    def test_no_used_chatroom_send_guard_on_synthesis(self):
        """The synthesis display must not be gated by _used_chatroom_send."""
        content = CYCLE_SRC.read_text(encoding='utf-8')
        idx = content.find("displayed {} synthesis output")
        self.assertGreater(idx, 0, "synthesis display marker found")
        block = content[max(0, idx - 800):idx + 200]
        self.assertNotIn("_used_chatroom_send", block,
            "Synthesis display block must not reference _used_chatroom_send")


# ── Test 3: Retry loop removed (stable-aligned) ────────────

class TestRetryLoopRemoved(unittest.TestCase):
    """The _MIN_SYNTHESIS_LEN + synthesis_quality_check retry loop was removed
    to eliminate end-of-discussion stalls. The max_cycles cap is the only
    backstop."""

    def test_no_min_synthesis_len_retry(self):
        content = BROADCAST.read_text(encoding='utf-8') + CYCLE_SRC.read_text(encoding='utf-8')
        self.assertNotIn("_MIN_SYNTHESIS_LEN", content,
            "_MIN_SYNTHESIS_LEN hard retry was removed (stable behavior)")

    def test_no_synthesis_quality_check(self):
        content = BROADCAST.read_text(encoding='utf-8') + CYCLE_SRC.read_text(encoding='utf-8')
        self.assertNotIn("synthesis_quality_check", content,
            "synthesis_quality_check retry loop was removed (stable behavior)")

    def test_no_inject_retry_helper(self):
        content = BROADCAST.read_text(encoding='utf-8') + CYCLE_SRC.read_text(encoding='utf-8')
        self.assertNotIn("_inject_retry", content,
            "_inject_retry helper was removed")

    def test_single_forced_synthesis_on_empty(self):
        """When leader ends with no text, exactly one forced cycle fires."""
        content = CYCLE_SRC.read_text(encoding='utf-8')
        idx = content.find("called end_discussion without text")
        self.assertGreater(idx, 0, "empty-synthesis force marker found")
        block = content[idx:idx + 800]
        self.assertIn("leader_end_without_text", block,
            "Forced synthesis uses the leader_end_without_text warning")
        self.assertIn("continue", block,
            "Forced synthesis re-enters tool_loop (single retry)")


# ── Test 4: Non-leader wait keeps waiting (no force-exit) ──

class TestWaitNoForceExit(unittest.TestCase):
    """MAX_CONSECUTIVE_WAITS force-exit was removed: non-leaders keep waiting
    on timeout (mailbox nudge + leader end_discussion are the exit paths)."""

    def test_no_max_consecutive_waits(self):
        content = BROADCAST.read_text(encoding='utf-8') + CYCLE_SRC.read_text(encoding='utf-8')
        self.assertNotIn("MAX_CONSECUTIVE_WAITS", content,
            "MAX_CONSECUTIVE_WAITS force-exit was removed")
        self.assertNotIn("_consecutive_waits", content,
            "_consecutive_waits counter was removed")

    def test_non_leader_keeps_waiting(self):
        content = CYCLE_SRC.read_text(encoding='utf-8')
        idx = content.find("Non-leader: keep waiting")
        self.assertGreater(idx, 0, "non-leader keep-waiting branch found")
        block = content[idx:idx + 500]
        self.assertIn("continue", block, "non-leader retries wait on timeout")


# ── Test 5: build_tool_log still works ─────────────────────

class TestBuildToolLog(unittest.TestCase):
    """Verify the tool log helper still produces output."""

    def test_build_tool_log_imports(self):
        from nanobot.groupchat.context.tool_log import build_tool_log
        result = build_tool_log([
            {"name": "web_search", "args": {"query": "AI news"}, "content": "result: Google I/O 2026 confirmed"}
        ])
        self.assertTrue(len(result) > 0, "build_tool_log produces non-empty output")
        self.assertIn("web_search", result, "Tool name appears in log")

    def test_build_tool_log_empty(self):
        from nanobot.groupchat.context.tool_log import build_tool_log
        result = build_tool_log([])
        self.assertEqual(result, "", "Empty calls → empty string")

    def test_build_tool_log_no_substantive(self):
        from nanobot.groupchat.context.tool_log import build_tool_log
        result = build_tool_log([
            {"name": "chatroom_send", "args": {"to": "All"}, "content": "hello"},
            {"name": "wait", "args": {}, "content": ""},
        ])
        self.assertIsInstance(result, str, "Always returns str")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
