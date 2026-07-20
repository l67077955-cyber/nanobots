"""Prefix-cache byte-stability regression tests.

Provider-side prefix caching keys on the exact byte prefix of the message
list. After the History refactor every cycle rebuilds the prompt from
History, so rebuilds must satisfy:

1. same History state → byte-identical rendering;
2. appending a turn → the new rendering is the old rendering + pure append
   (older messages keep their exact bytes, even when later merged);
3. aging rewrites old tool logs in rare batches, not every cycle.

Incident: a leading "\\n\\n" in build_tool_log + .strip() in
_merge_consecutive_assistant rewrote a mid-history assistant message on
almost every rebuild (observed divergence points wandering idx 29..44 in
request logs), so the prefix cache could never hit beyond the static head.
"""

from __future__ import annotations

import json

from nanobot.core.history import History
from nanobot.groupchat.runtime.working_memory import commit_agent_turn


class _FakeEngine:
    def __init__(self) -> None:
        self.history = History()

    def _persist_after_history_write(self, sender: str, content: str) -> None:
        pass


def _tool_detail(name: str = "exec", preview: str = "ok") -> list[dict]:
    return [{
        "name": name,
        "args": "{}",
        "result_preview": preview,
        "result_len": len(preview),
        "success": True,
    }]


def _dump(msgs: list[dict]) -> list[str]:
    return [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in msgs]


def test_commit_without_text_has_no_leading_whitespace():
    """Empty content + tool log must not store a leading '\\n\\n'."""
    eng = _FakeEngine()
    commit_agent_turn(eng, "Kirk", None, _tool_detail())
    stored = eng.history[-1].content
    assert stored.startswith("<previous_tool_calls>")
    assert stored == stored.strip()


def test_commit_with_text_uses_single_blank_line_separator():
    eng = _FakeEngine()
    commit_agent_turn(eng, "Kirk", "  结论先行  ", _tool_detail())
    stored = eng.history[-1].content
    assert stored.startswith("结论先行\n\n<previous_tool_calls>")
    assert stored == stored.strip()


def test_merge_preserves_previous_bytes_verbatim():
    """Merging consecutive assistant msgs appends; it never rewrites prev."""
    h = History()
    h.agent("Kirk", "<previous_tool_calls>\n• exec() → ok (2字)\n</previous_tool_calls>")
    h.agent("Kirk", "第二轮发言")
    msgs = [m for m in h.build_for_groupchat("Kirk") if m["role"] == "assistant"]
    assert len(msgs) == 1
    first = "<previous_tool_calls>\n• exec() → ok (2字)\n</previous_tool_calls>"
    assert msgs[0]["content"] == f"{first}\n\n第二轮发言"


def test_rebuild_is_pure_append_after_new_turn():
    """The scenario from the incident: rebuild N vs N+1 share the full prefix."""
    h = History()
    h.user("部署愤怒的小鸟")
    commit_agent_turn(_attach(h), "Kirk", None, _tool_detail(preview="stored"))
    h_before = h.build_for_groupchat("Kirk")
    base = _dump(h_before)
    assert base  # sanity

    # New adjacent same-agent turn → triggers the assistant merge path.
    commit_agent_turn(_attach(h), "Kirk", "补充说明", _tool_detail(preview="more"))
    grown = _dump(h.build_for_groupchat("Kirk"))

    # Every previously rendered message must survive as an exact content
    # prefix of its counterpart in the new rendering (merge only appends).
    old_msgs = h_before
    new_msgs = h.build_for_groupchat("Kirk")
    assert len(new_msgs) >= len(old_msgs)
    for old, new in zip(old_msgs, new_msgs):
        assert old.get("role") == new.get("role")
        assert old.get("name") == new.get("name")
        assert (new.get("content") or "").startswith(old.get("content") or "")


def test_rebuild_identical_for_same_state():
    h = History()
    h.user("q")
    commit_agent_turn(_attach(h), "Kirk", "答案", _tool_detail())
    a = _dump(h.build_for_groupchat("Kirk"))
    b = _dump(h.build_for_groupchat("Kirk"))
    assert a == b


def test_age_tools_batches_instead_of_moving_front():
    """Aging must not rewrite one mid-history message per cycle."""
    h = History()
    h.append("system_prompt", "sys")
    long_preview = "x" * 150
    block = f"\n\n[工具调用记录]\n• search(q) → {long_preview} (10字)"
    h.agent("Kirk", "t1" + block)  # idx1
    h.agent("Kirk", "t2" + block)  # idx2
    keep_recent = 2

    # total=3 → frontier=1, batch (1-0) < 2 → no-op.
    assert h.age_tools(keep_recent=keep_recent) == 0
    assert h[1].content.count("x") == 150

    # total=4 → frontier=2, batch (2-0) >= 2 → first batch fires, ages t1.
    h.agent("Kirk", "t3" + block)  # idx3
    assert h.age_tools(keep_recent=keep_recent) == 1
    assert h[1].content.count("x") == 100
    assert h[2].content.count("x") == 150

    # total=5 → frontier=3, batch (3-2) < 2 → no-op: zero mid-history churn.
    h.agent("Kirk", "t4" + block)  # idx4
    assert h.age_tools(keep_recent=keep_recent) == 0
    assert h[2].content.count("x") == 150

    # total=6 → frontier=4, batch (4-2) >= 2 → second batch ages t2+t3.
    h.agent("Kirk", "t5" + block)  # idx5
    assert h.age_tools(keep_recent=keep_recent) == 2
    assert h[2].content.count("x") == 100
    assert h[3].content.count("x") == 100
    assert h[4].content.count("x") == 150  # still inside keep window


def test_age_tools_watermark_resets_after_clear():
    h = History()
    long_preview = "x" * 150
    h.append("system_prompt", "sys")  # idx0 head-protected
    h.agent("Kirk", f"a\n\n[工具调用记录]\n• s(q) → {long_preview} (10字)")
    h.agent("Kirk", "b")
    # total=3 → frontier=2, batch (2-0) >= 1 → fires, ages idx1 only.
    assert h.age_tools(keep_recent=1) == 1
    h.clear()
    assert h._aged_upto == 0


def _attach(h: History):
    """Fake engine bound to an existing History."""
    eng = _FakeEngine()
    eng.history = h
    return eng
