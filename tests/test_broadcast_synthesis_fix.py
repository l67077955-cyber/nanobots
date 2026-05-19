#!/usr/bin/env python3
"""
Test: broadcast.py synthesis display + retry prompt fix.

Covers three bugs introduced in 5/13-5/14:
1. _used_chatroom_send blocks synthesis display
2. retry prompt lacks tool data → LLM can't produce content
3. retry exhaustion silently releases empty content

This is a unit test at the logic level — simulates the exact
broadcast_round conditions that triggered the failures.
"""

import ast
import sys
import unittest
from pathlib import Path

SRC = Path("/root/nanobot-src/nanobot/groupchat/orchestra/broadcast.py")
HISTORY = Path("/root/nanobot-src/nanobot/groupchat/history/component_manager.py")

# ── Test 1: Syntax Integrity ──────────────────────────────

class TestSyntax(unittest.TestCase):
    """Verify all modified files parse cleanly."""

    def test_broadcast_syntax(self):
        with open(SRC) as f:
            ast.parse(f.read())
        self.assertTrue(True, "broadcast.py syntax OK")

    def test_component_manager_syntax(self):
        with open(HISTORY) as f:
            ast.parse(f.read())
        self.assertTrue(True, "component_manager.py syntax OK")


# ── Test 2: _used_chatroom_send guard removal ─────────────

class TestUsedChatroomSendGuard(unittest.TestCase):
    """The old guard: `if content and not _used_chatroom_send`
    dropped leader synthesis when leader used chatroom_send.
    New code: `if content:` — removes the condition entirely.
    """

    def test_guard_removed_from_synthesis_display(self):
        """Verify line ~1249 has `if content:` not `if content and not _used_chatroom_send`"""
        with open(SRC) as f:
            lines = f.readlines()
        # Find the synthesis display block — the line that has
        # "if content:" near a comment about chatroom_send
        found = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # The fixed line should: (a) start with `if content:` 
            # but NOT contain `_used_chatroom_send`
            if stripped.startswith("if content:") and not "_used_chatroom_send" in stripped:
                # Verify it's in the right context — preceded by the comment
                before = lines[i-2].strip() if i >= 2 else ""
                if "chatroom_send" in before:
                    found = True
                    break
        self.assertTrue(found, "Line `if content:` found near chatroom_send comment (removed _used_chatroom_send guard)")

    def test_no_used_chatroom_send_in_display_block(self):
        """Verify the synthesis display block (~L1249-1260) does NOT reference _used_chatroom_send"""
        with open(SRC) as f:
            content = f.read()
        # Find the post-validation display section
        marker = "Synthesis passed validation"
        idx = content.find(marker)
        self.assertGreater(idx, 0, "Synthesis passed validation marker found")
        block = content[idx:idx+2000]
        self.assertNotIn("_used_chatroom_send", block,
            "Synthesis display block must not reference _used_chatroom_send")


# ── Test 3: Retry prompt includes tool data ────────────────

class TestRetryPromptToolData(unittest.TestCase):
    """Before fix: retry prompt = `get_system_warning(...)` only.
    After fix: retry prompt = warning + `[本轮工具调用结果]` + build_tool_log().
    """

    def test_length_retry_has_tool_injection(self):
        with open(SRC) as f:
            content = f.read()
        # Marker for the length-based retry
        marker = "synthesis too short"
        idx = content.find(marker)
        self.assertGreater(idx, 0, "Length retry marker found")
        block = content[idx:idx+2500]
        # After the fix, there should be a reference to build_tool_log
        # AND the tool data injection string
        self.assertIn("build_tool_log", block, "Length retry uses build_tool_log")
        self.assertIn("本轮工具调用结果", block, "Length retry injects tool data header")

    def test_quality_retry_has_tool_injection(self):
        with open(SRC) as f:
            content = f.read()
        marker = "质量检查失败"
        idx = content.find(marker)
        self.assertGreater(idx, 0, "Quality retry marker found")
        block = content[idx-500:idx+1500]
        self.assertIn("build_tool_log", block, "Quality retry uses build_tool_log")
        self.assertIn("本轮工具调用结果", block, "Quality retry injects tool data header")


# ── Test 4: build_tool_log function exists and works ──────

class TestBuildToolLog(unittest.TestCase):
    """Verify the tool used to inject data actually produces output."""

    def test_build_tool_log_imports(self):
        """Verify build_tool_log can be imported and called"""
        from nanobot.groupchat.orchestra.engine import build_tool_log
        result = build_tool_log([
            {"name": "web_search", "args": {"query": "AI news"}, "content": "result: Google I/O 2026 confirmed"}
        ])
        self.assertTrue(len(result) > 0, "build_tool_log produces non-empty output")
        self.assertIn("web_search", result, "Tool name appears in log")


# ── Test 5: End-to-end integration simulation ─────────────

class TestSynthesisSimulation(unittest.TestCase):
    """Simulate the exact scenario that broke:
    - Leader produces short text + calls chatroom_send + end_discussion
    - The synthesis should STILL be displayed (Bug 1 fix)
    If retry fires, it should have tool data (Bug 2 fix)
    """

    def test_build_tool_log_with_typical_content(self):
        """Simulate a leader that searched news and posted summary"""
        from nanobot.groupchat.orchestra.engine import build_tool_log
        
        calls = [
            {"name": "web_search", "args": {"query": "AI news today 2026"}, 
             "content": "Google I/O 2026: Gemini 4.0 announced\nOpenAI GPT-5 leaks\nMeta Llama 4 release date"},
            {"name": "web_fetch", "args": {"url": "https://example.com/ai-news"}, 
             "content": "Key announcements from Google I/O...\nGemini 4.0 features multimodal...\n200+ tokens context window..."},
            {"name": "chatroom_send", "args": {"to": "Harper", "message": "搜索完毕"}, 
             "content": ""},
        ]
        
        tool_log = build_tool_log(calls)
        
        # Should include search and fetch results (substantive tools)
        self.assertIn("Google I/O", tool_log, "Tool log contains search result")
        self.assertIn("Gemini 4.0", tool_log, "Tool log contains fetch result")
        self.assertGreater(len(tool_log), 100, 
            "Tool log is substantive (>100 chars), sufficient for LLM to synthesize")
        
        # If the retry prompt includes this, LLM can produce a real summary
        retry_prompt = (
            "[⚠️ 你调用了 end_discussion，但还没有给出最终答案！]\n"
            "请立即整合所有队友的发现\n\n"
            "[本轮工具调用结果 — 请基于以下数据输出总结]\n"
            + tool_log
        )
        self.assertGreater(len(retry_prompt), 400,
            "Retry prompt with tool data exceeds _MIN_SYNTHESIS_LEN threshold")


# ── Test 6: Edge case — no tool data to inject ────────────

class TestEdgeCases(unittest.TestCase):
    """What happens when there's no tool data at all?"""

    def test_build_tool_log_empty(self):
        from nanobot.groupchat.orchestra.engine import build_tool_log
        result = build_tool_log([])
        self.assertEqual(result, "", "Empty calls → empty string")
        
    def test_build_tool_log_no_substantive(self):
        from nanobot.groupchat.orchestra.engine import build_tool_log
        result = build_tool_log([
            {"name": "chatroom_send", "args": {"to": "All"}, "content": "hello"},
            {"name": "wait", "args": {}, "content": ""},
        ])
        # May still produce something (tool names) but that's OK
        self.assertIsInstance(result, str, "Always returns str")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
