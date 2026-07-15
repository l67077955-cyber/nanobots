"""Working memory + History commit seam tests."""

from __future__ import annotations

from types import SimpleNamespace

from nanobot.core.history import History
from nanobot.groupchat.orchestra.working_memory import WorkingMemory, commit_agent_turn


class _FakeEngine:
    def __init__(self) -> None:
        self.history = History()
        self.added: list[tuple[str, str]] = []

    def _add_message(self, sender: str, content: str) -> None:
        self.added.append((sender, content))
        self.history.add_from_sender(sender, content)


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
    assert eng.added[0][0] == "Harper"
    assert eng.history.last_sender() == "Harper"
    assert "done" in eng.history[-1].content


def test_commit_agent_turn_skips_empty():
    eng = _FakeEngine()
    assert commit_agent_turn(eng, "Harper", None, None) == ""
    assert eng.added == []
    assert len(eng.history) == 0


def test_working_memory_insert_before_last_tracks_injections():
    wm = WorkingMemory(messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ])
    wm.insert_before_last({"role": "system", "content": "leader"})
    wm.insert_before_last({"role": "system", "content": "perm"})
    # reverse order in front of tail (matches broadcast insert semantics)
    # insert always before last → earlier inserts sit further left
    assert [m["content"] for m in wm.messages] == ["sys", "leader", "perm", "hi"]
    assert [m["content"] for m in wm.role_injections] == ["leader", "perm"]


def test_working_memory_refresh_rebuilds_from_history_prompt():
    eng = _FakeEngine()
    eng.history.add_from_sender("用户", "q1")
    eng.history.add_from_sender("Harper", "a1")

    def build():
        # minimal stand-in for _build_agent_prompt: History → groupchat view
        return eng.history.build_for_groupchat(current_agent="Harper")

    wm = WorkingMemory(messages=build())
    wm.insert_before_last({"role": "system", "content": "role-hint"})

    # teammate commits while we "wait"
    eng.history.add_from_sender("Kirk", "teammate update")

    trailing = [{"role": "user", "content": "[队友消息] Kirk: hello"}]
    msgs = wm.refresh(build, trailing=trailing)

    texts = " ".join(str(m.get("content", "")) for m in msgs)
    assert "a1" in texts
    assert "teammate update" in texts or "[Kirk]" in texts
    assert any(m.get("content") == "role-hint" for m in msgs)
    assert msgs[-1]["content"].startswith("[队友消息]")
    # role injection still tracked for next refresh
    assert wm.role_injections[0]["content"] == "role-hint"


def test_history_add_from_sender_public():
    h = History()
    h.add_from_sender("用户", "hello")
    h.add_from_sender("Harper", "world")
    assert h.count_by_attr("role", "user") == 1
    assert h.count_by_attr("role", "assistant") == 1
    assert h.latest_user_content() == "hello"
