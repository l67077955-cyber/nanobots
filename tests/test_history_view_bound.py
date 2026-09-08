"""C1.1 (plan.md 批次 C1 第 1 条): per-agent views are bounded.

``add_message`` enforced its message-count / char-budget caps only on the log
(``self.messages``); the per-agent ``_views[name]`` grew without bound, and
with AI summarisation disabled ``_compress_view`` deliberately keeps the
middle region — so a view was strictly append-only.  These tests pin that the
same two-step cap now applies to every **materialised** view, with the same
head-protection semantics as ``_compress_view`` (the first message and user
messages survive), while the log's own trimming semantics stay unchanged.

Unmaterialised views (agent not yet active) need no handling: ``view_for``
projects them live from the already-bounded log.
"""

from __future__ import annotations

import pytest

from nanobot.groupchat.history import history_settings
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState


def _make_context(tmp_path, provider=None) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=provider)


@pytest.fixture()
def bounded_settings(monkeypatch):
    """Small window so bound violations show up after a few dozen writes."""
    monkeypatch.setattr(history_settings, "max_messages", lambda: 10)
    monkeypatch.setattr(history_settings, "max_context_chars", lambda: 0)
    monkeypatch.setattr(history_settings, "compress_ratio", lambda: 0.8)
    monkeypatch.setattr(history_settings, "compression_keep_recent", lambda: 6)
    monkeypatch.setattr(history_settings, "history_summarize_enabled", lambda: False)
    monkeypatch.setattr(history_settings, "compress_max_summary_tokens", lambda: 600)
    monkeypatch.setattr(history_settings, "keep_user_messages", lambda: False)
    return bounded_settings


class _FakeProvider:
    """Counts calls; summarisation is disabled in these tests anyway."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, messages, model=None, max_tokens=None, **kw):
        self.calls += 1

        class _R:
            content = "[压缩摘要] fake"
            finish_reason = "stop"

        return _R()


class TestViewMessageCountBound:
    def test_view_bounded_with_summarisation_disabled(self, tmp_path, bounded_settings):
        """Plan acceptance: 摘要禁用 + 持续写入 → len(view) ≤ max_messages
        (plus the head-protected first message that may sit outside the kept
        tail — the exact semantics the log itself has), and the first message
        survives."""
        ctx = _make_context(tmp_path)
        ctx.set_active_agents(["A", "B", "C"])
        for i in range(30):
            ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")

        for name in ("A", "B", "C"):
            view = ctx.view_for(name)
            # tail(10) + head-protected first message outside the tail window
            assert len(view) == 11, (
                f"{name}'s view must be bounded to the max_messages tail "
                f"(+ protected head), got {len(view)}"
            )
            assert view[0]["content"] == "msg0", "first message must survive view trimming"
        # The log itself keeps its pre-existing semantics
        assert len(ctx.messages) == 11

    def test_user_messages_survive_view_trimming(self, tmp_path, bounded_settings, monkeypatch):
        """keep_user_messages=True protects ALL user messages in views too —
        the same head protection _compress_view applies."""
        monkeypatch.setattr(history_settings, "keep_user_messages", lambda: True)
        ctx = _make_context(tmp_path)
        ctx.set_active_agents(["A", "B"])
        user_indices = {0, 5, 15, 25, 35}
        for i in range(40):
            ctx.add_message("用户" if i in user_indices else "系统", f"msg{i}")

        view = ctx.view_for("A")
        contents = [m["content"] for m in view]
        for i in sorted(user_indices):
            assert f"msg{i}" in contents, f"user message msg{i} must survive"
        assert view[0]["content"] == "msg0"
        # tail(10) + 4 protected users outside the tail window (msg35 is in it)
        assert len(view) == 14

    def test_unmaterialised_view_projects_from_bounded_log(self, tmp_path, bounded_settings):
        """An agent that never became active has no stored view; its live
        projection comes from the already-bounded log."""
        ctx = _make_context(tmp_path)
        ctx.set_active_agents(["A"])
        for i in range(30):
            ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")

        assert len(ctx.view_for("C")) == len(ctx.messages) == 11


class TestViewCharBudgetBound:
    def test_view_trimmed_to_char_budget(self, tmp_path, monkeypatch):
        """The char-budget step bounds views as well: head is counted but
        always kept, non-head messages are dropped oldest-first."""
        monkeypatch.setattr(history_settings, "max_messages", lambda: 1000)
        monkeypatch.setattr(history_settings, "max_context_chars", lambda: 300)
        monkeypatch.setattr(history_settings, "keep_user_messages", lambda: True)
        monkeypatch.setattr(history_settings, "history_summarize_enabled", lambda: False)
        ctx = _make_context(tmp_path)
        ctx.set_active_agents(["A", "B"])
        ctx.add_message("用户", "H" * 100)  # head (first + user)
        for i in range(10):
            ctx.add_message("系统", f"{i:03d}" + "x" * 97)  # 100 chars each

        for name in ("A", "B"):
            view = ctx.view_for(name)
            total = sum(len(m["content"]) for m in view)
            assert total == 300, f"{name}: char budget must bound the view, got {total}"
            assert view[0]["content"] == "H" * 100
            assert len(view) == 3  # head + the two newest that fit


class TestLogSemanticsUnchanged:
    def test_view_trimming_does_not_change_log_trimming(self, tmp_path, bounded_settings):
        """With and without materialised views, the log must trim identically
        (view trimming is purely additive, it must not feed back into the log)."""
        with_views = _make_context(tmp_path / "w")
        without_views = _make_context(tmp_path / "o")
        with_views.set_active_agents(["A", "B", "C"])
        for i in range(30):
            sender = "用户" if i == 0 else "系统"
            with_views.add_message(sender, f"msg{i}")
            without_views.add_message(sender, f"msg{i}")

        assert with_views.messages == without_views.messages

    def test_private_target_views_are_bounded_too(self, tmp_path, bounded_settings):
        """Views whose content differs from the log (private targets) are
        bounded with their own head/tail, not the log's message identities."""
        ctx = _make_context(tmp_path)
        ctx.set_active_agents(["A", "B", "C"])
        for i in range(30):
            ctx.add_message("用户" if i == 0 else "系统", f"msg{i}")
        # Private C-only traffic inflates C's view past the others
        for i in range(25):
            ctx.add_message("A", f"private-{i}", targets=["C"])

        c_view = ctx.view_for("C")
        assert len(c_view) == 11
        assert c_view[0]["content"] == "msg0"
        assert c_view[-1]["content"] == "private-24", "newest private message kept"
