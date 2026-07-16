"""Working memory + History commit seam tests.

History is the sole durable context layer; WorkingMemory is ephemeral.
"""

from __future__ import annotations

from nanobot.core.history import History
from nanobot.groupchat.runtime.working_memory import WorkingMemory, commit_agent_turn


class _FakeEngine:
    """Minimal engine: History + optional persist hook (no parallel store)."""

    def __init__(self) -> None:
        self.history = History()
        self.persisted: list[tuple[str, str]] = []

    def _persist_after_history_write(self, sender: str, content: str) -> None:
        self.persisted.append((sender, content))


def test_commit_agent_turn_appends_tool_log():
    eng = _FakeEngine()
    out = commit_agent_turn(
        eng,
        "Harper",
        "done",
        [{"name": "web_search", "args": "{}", "result_preview": "hit", "result_len": 3, "success": True}],
    )
    assert out.startswith("done")
    assert "previous_tool_calls" in out or "web_search" in out
    # Context write is History-only
    assert eng.history.last_sender() == "Harper"
    assert "done" in eng.history[-1].content
    # I/O hook observed
    assert eng.persisted[0][0] == "Harper"
    assert eng.persisted[0][1].startswith("done")


def test_commit_agent_turn_skips_empty():
    eng = _FakeEngine()
    assert commit_agent_turn(eng, "Harper", None, None) == ""
    assert eng.persisted == []
    assert len(eng.history) == 0


def test_working_memory_insert_before_last_tracks_injections():
    wm = WorkingMemory(messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ])
    wm.insert_before_last({"role": "system", "content": "leader"})
    wm.insert_before_last({"role": "system", "content": "perm"})
    assert [m["content"] for m in wm.messages] == ["sys", "leader", "perm", "hi"]
    assert [m["content"] for m in wm.role_injections] == ["leader", "perm"]


def test_working_memory_refresh_rebuilds_from_history_prompt():
    eng = _FakeEngine()
    eng.history.add_from_sender("用户", "q1")
    eng.history.add_from_sender("Harper", "a1")

    def build():
        return eng.history.build_for_groupchat(current_agent="Harper")

    wm = WorkingMemory(messages=build())
    wm.insert_before_last({"role": "system", "content": "role-hint"})

    eng.history.add_from_sender("Kirk", "teammate update")

    trailing = [{"role": "user", "content": "[队友消息] Kirk: hello"}]
    msgs = wm.refresh(build, trailing=trailing)

    texts = " ".join(str(m.get("content", "")) for m in msgs)
    assert "a1" in texts
    assert "teammate update" in texts or "[Kirk]" in texts
    assert any(m.get("content") == "role-hint" for m in msgs)
    assert msgs[-1]["content"].startswith("[队友消息]")
    assert wm.role_injections[0]["content"] == "role-hint"
