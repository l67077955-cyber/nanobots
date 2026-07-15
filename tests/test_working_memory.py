"""Working memory + History commit seam tests."""

from __future__ import annotations

from types import SimpleNamespace

from nanobot.core.history import History
from nanobot.groupchat.runtime.working_memory import WorkingMemory, commit_agent_turn


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


def test_interrupt_style_refresh_trailing():
    """Interrupt re-entry: History holds partial turn; trailing carries latest interrupt."""
    eng = _FakeEngine()
    eng.history.add_from_sender("用户", "start")
    eng.history.add_from_sender("Harper", "partial thought")

    def build():
        return eng.history.build_for_groupchat(current_agent="Harper")

    wm = WorkingMemory(messages=build())
    wm.insert_before_last({"role": "system", "content": "role-hint"})

    # teammate spoke while we were mid-tool_loop
    eng.history.add_from_sender("Kirk", "urgent update")

    trailing = [
        {
            "role": "system",
            "content": "[打断期间积压的 1 条较早消息（仅供参考）]\n- [Kirk]: stale\n请重点关注下面的最新消息。",
        },
        {
            "role": "user",
            "content": "[Kirk — 最新消息]: urgent update",
        },
    ]
    msgs = wm.refresh(build, trailing=trailing)
    blob = " ".join(str(m.get("content", "")) for m in msgs)
    assert "partial thought" in blob
    assert "urgent update" in blob or "[Kirk]" in blob
    assert any("最新消息" in str(m.get("content", "")) for m in msgs)
    assert any(m.get("content") == "role-hint" for m in msgs)


def test_volatile_index_after_refresh_with_trailing():
    eng = _FakeEngine()
    eng.history.add_from_sender("用户", "q")
    eng.history.add_from_sender("Harper", "a")

    def build():
        # mimic prompt builder tail: static + volatile user
        msgs = eng.history.build_for_groupchat(current_agent="Harper")
        msgs.append({"role": "user", "content": "[Current date]"})
        return msgs

    wm = WorkingMemory(messages=build())
    assert wm.trailing_count == 0
    assert wm.messages[wm.volatile_index]["content"] == "[Current date]"

    wm.insert_before_last({"role": "system", "content": "hint"})
    # still last is volatile
    assert wm.messages[wm.volatile_index]["content"] == "[Current date]"

    eng.history.add_from_sender("Kirk", "update")
    msgs = wm.refresh(
        build,
        trailing=[
            {"role": "system", "content": "nudge"},
            {"role": "user", "content": "[队友消息] hi"},
        ],
    )
    assert wm.trailing_count == 2
    assert wm.messages[wm.volatile_index]["content"] == "[Current date]"
    assert msgs[-1]["content"].startswith("[队友消息]")
    assert msgs[-2]["content"] == "nudge"
    # status target still the volatile user msg
    vi = wm.volatile_index
    wm.messages[vi]["content"] += "\n### [本轮状态汇总]"
    assert "本轮状态汇总" in wm.messages[vi]["content"]
    assert "本轮状态汇总" not in msgs[-1]["content"]


def test_reenter_helper():
    eng = _FakeEngine()
    eng.history.add_from_sender("用户", "q")

    def build():
        return [{"role": "user", "content": "v"}]

    wm = WorkingMemory(messages=build())
    out = wm.reenter(build, {"role": "system", "content": "idle-nudge"})
    assert out[-1]["content"] == "idle-nudge"
    assert wm.trailing_count == 1
    assert wm.volatile_index == 0
