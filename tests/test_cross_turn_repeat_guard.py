"""Regression tests for the cross-turn repetition guard.

The guard (context/repetition.py) was disconnected during the
runtime/context/display refactor: settings and the telegram panel toggle
survived, but no call site invoked the check. It is now re-connected at
``commit_agent_turn`` (the sole agent-speech write path into History).
"""

from __future__ import annotations

import pytest
from loguru import logger

from nanobot.core.history import History
from nanobot.groupchat.context import history_settings as hs
from nanobot.groupchat.context.repetition import (
    is_cross_turn_repeat,
    warn_if_cross_turn_repeat,
)
from nanobot.groupchat.runtime.working_memory import commit_agent_turn


# Must exceed repetition._MIN_MEANINGFUL_LEN (40 chars) to be checkable.
_LONG = "这一轮我完成了对压缩管线的全面审查，确认所有快照测试均通过，没有发现任何回归问题，可以进入下一阶段。"
assert len(_LONG) >= 40


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setattr(hs, "cross_turn_repeat_guard", lambda: True)
    monkeypatch.setattr(hs, "cross_turn_repeat_ratio", lambda: 0.85)


@pytest.fixture
def guard_off(monkeypatch):
    monkeypatch.setattr(hs, "cross_turn_repeat_guard", lambda: False)


class _ListHandler:
    def __init__(self):
        self.records: list[str] = []

    def write(self, message):
        self.records.append(message)


@pytest.fixture
def warning_sink():
    sink = _ListHandler()
    handler_id = logger.add(sink.write, level="WARNING", format="{message}")
    try:
        yield sink
    finally:
        logger.remove(handler_id)


def test_guard_disabled_means_no_warning(guard_off, warning_sink):
    h = History()
    h.commit_turn("isaac", _LONG)
    warn_if_cross_turn_repeat(h, "isaac", _LONG)
    assert not any("cross-turn repeat" in r for r in warning_sink.records)


def test_guard_warns_on_repeat(guard_on, warning_sink):
    h = History()
    h.commit_turn("isaac", _LONG)
    warn_if_cross_turn_repeat(h, "isaac", _LONG + "（补充：无）")
    assert any("cross-turn repeat by isaac" in r for r in warning_sink.records)


def test_guard_ignores_other_agents_and_short_messages(guard_on, warning_sink):
    h = History()
    h.commit_turn("other", _LONG)
    # different agent speaking → no prior message for "isaac"
    warn_if_cross_turn_repeat(h, "isaac", _LONG)
    # short message below _MIN_MEANINGFUL_LEN never trips even when identical
    h.commit_turn("isaac", "好的")
    warn_if_cross_turn_repeat(h, "isaac", "好的")
    assert not any("cross-turn repeat" in r for r in warning_sink.records)


def test_guard_never_raises_when_settings_broken(monkeypatch):
    def _boom():
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(hs, "cross_turn_repeat_guard", _boom)
    h = History()
    h.commit_turn("isaac", _LONG)
    warn_if_cross_turn_repeat(h, "isaac", _LONG)  # must not raise


def test_guard_wired_into_commit_agent_turn(guard_on, warning_sink):
    """End-to-end: agent speech committed via commit_agent_turn is checked."""

    class _Engine:
        def __init__(self, history):
            self.history = history
            self.persisted: list[tuple[str, str]] = []

        def _persist_after_history_write(self, sender, content):
            self.persisted.append((sender, content))

    engine = _Engine(History())
    commit_agent_turn(engine, "isaac", _LONG)
    assert not any("cross-turn repeat" in r for r in warning_sink.records)

    # Second near-identical turn by the same agent trips the guard.
    commit_agent_turn(engine, "isaac", _LONG + "（同上）")
    assert any("cross-turn repeat by isaac" in r for r in warning_sink.records)
    # Both turns still committed — the guard is observational, never blocks.
    assert [s for s, _ in engine.persisted] == ["isaac", "isaac"]


def test_is_cross_turn_repeat_threshold():
    repeated, score = is_cross_turn_repeat(_LONG, _LONG, threshold=0.85)
    assert repeated and score == pytest.approx(1.0)
    repeated, _ = is_cross_turn_repeat(_LONG, "完全不同的内容，没有任何重叠的句子存在。", 0.85)
    assert not repeated
