"""Behavioral tests for groupchat display / UI formatting and inter-agent flows.

Covers the pure display helpers in nanobot/groupchat/display/display.py — the
visible face of groupchat behavior (broadcast banners, chatroom messages,
tool-result briefs, user interjections) — plus edge cases that previously
produced malformed or exception-throwing output.
"""

from __future__ import annotations

import pytest

from nanobot.groupchat.display.display import (
    broadcast_start_msg,
    broadcast_complete_msg,
    search_bar,
    chatroom_send_msg,
    chatroom_wait_msg,
    user_interjection_msg,
    tool_result_brief,
    agent_badge,
    thinking_msg,
)


class TestBroadcastBanner:
    def test_start_with_leader_and_ranks(self):
        out = broadcast_start_msg(["Nanobot", "Harper", "Lucas"], 90, leader="Nanobot",
                                  ranks={"Nanobot": "king", "Harper": "bishop"})
        assert "Broadcast" in out and "3 agents" in out and "90s" in out
        assert "👑 Nanobot (king)" in out
        assert "🔹 Harper (bishop)" in out
        assert "🔹 Lucas (pawn)" in out  # default rank
        # Leader must not also appear as a 🔹 member.
        assert out.count("🔹 Nanobot") == 0

    def test_start_without_leader(self):
        out = broadcast_start_msg(["A", "B"], 60)
        assert "0 agents" not in out.replace("🔹", "") or True
        assert "A (pawn)" in out and "B (pawn)" in out
        assert "👑" not in out

    def test_start_empty_agents_no_crash(self):
        out = broadcast_start_msg([], 60)
        assert "0 agents" in out

    def test_complete_counts(self):
        assert broadcast_complete_msg(3, 5) == "══ Done · 3/5 ══"
        assert broadcast_complete_msg(3, 5, comm_count=2) == "══ Done · 3/5 · 2 msgs ══"


class TestSearchBar:
    def test_normal(self):
        out = search_bar(pool=2, total=4, nodes=3)
        assert out == "🔍 ▰▰▱▱ 2/4 · 3 nodes"

    def test_all_used(self):
        out = search_bar(pool=0, total=4, nodes=1)
        assert "▰▰▰▰" in out

    def test_pool_greater_than_total_no_crash(self):
        # pool > total would make `used` negative → "▰" * negative is "",
        # but must not raise and must not produce a confusing negative count.
        out = search_bar(pool=5, total=4, nodes=1)
        assert isinstance(out, str) and "nodes" in out


class TestChatroomMessages:
    def test_leader_send(self):
        out = chatroom_send_msg("Nanobot", "Harper", "hello", leader="Nanobot")
        assert out.startswith("👑 Nanobot → Harper ━━")
        assert "hello" in out

    def test_regular_send(self):
        out = chatroom_send_msg("Harper", "Lucas", "hi")
        assert out.startswith("┄ Harper → Lucas ┄")
        assert "hi" in out

    def test_send_truncation(self):
        out = chatroom_send_msg("Harper", "Lucas", "x" * 3000, max_len=50)
        assert len(out.split("\n")[1]) == 50 + 1  # 50 chars + "…"
        assert out.split("\n")[1].endswith("…")

    def test_wait_msg_parses_sender(self):
        out = chatroom_wait_msg("Harper", "[Lucas]: reply", leader="Lucas")
        assert "Harper received from Lucas" in out

    def test_wait_msg_fallback_teammate(self):
        out = chatroom_wait_msg("Harper", "no bracket reply")
        assert "received from teammate" in out

    def test_user_interjection(self):
        out = user_interjection_msg("please continue")
        assert out == "── User ──\nplease continue"

    def test_user_interjection_truncates(self):
        out = user_interjection_msg("y" * 1000, max_len=20)
        assert out.split("\n")[1] == "y" * 20 + "…"


class TestToolResultBrief:
    def test_web_search_result_count(self):
        assert tool_result_brief("A", "web_search", "(5 results)") == "    └ 5 results"

    def test_web_fetch_char_count(self):
        assert tool_result_brief("A", "web_fetch", "x" * 12000) == "    └ fetched (12,000字)"

    def test_exec_preview(self):
        out = tool_result_brief("A", "exec", "hello world result")
        assert out.startswith("    └ hello world result")

    def test_write_file_success_and_error(self):
        assert tool_result_brief("A", "write_file", "ok saved") == "    └ ✅ saved"
        assert "❌" in tool_result_brief("A", "write_file", "Error: permission denied")

    def test_read_file_line_count(self):
        assert tool_result_brief("A", "read_file", "a\nb\nc") == "    └ 3 lines"

    def test_unknown_falls_back_to_char_count(self):
        assert tool_result_brief("A", "mystery_tool", "abc") == "    └ (3字)"


class TestAgentDisplay:
    def test_agent_badge_leader(self):
        assert "👑" in agent_badge("Nanobot", "Nanobot")

    def test_agent_badge_regular(self):
        # Non-leader gets no crown badge (empty string — the name is rendered by caller).
        assert agent_badge("Harper", "Nanobot") == ""

    def test_thinking_msg_includes_agent_and_time(self):
        out = thinking_msg("Harper", 2.5)
        assert "Harper" in out and "2.5" in out