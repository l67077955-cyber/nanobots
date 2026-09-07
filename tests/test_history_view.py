"""Phase B regression tests: per-agent visibility views (read-only projection).

Pins the contract that ``view_for(agent_name)`` returns exactly the messages
that agent is allowed to see, computed from each message's ``targets``:

  - ``targets=["All"]``  → visible to every agent
  - ``targets=["B"]``     → visible to B and to the sender (A sees its own send)
  - user / system msgs (default All) → visible to every agent

This is the step that fixes the "C 看到 A→B 私聊" bug: each agent's prompt
is built from its own view, not the full shared log.

Phase B views are a fresh projection over the log — mutating the returned
list must not corrupt the log, and a message added to the log after a view
was taken is visible on the next ``view_for`` call.  Compression still runs
the old shared ``maybe_compress`` (per-agent compression is Phase D).
"""

from __future__ import annotations

from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState


def _make_context(tmp_path) -> HistoryContext:
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=None)


# ─────────────────────────────────────────────────────────────────────────
# Visibility rules — the core privacy invariant
# ─────────────────────────────────────────────────────────────────────────


class TestVisibility:
    def test_private_message_visible_only_to_sender_and_target(self, tmp_path):
        """REGRESSION: A→B(targets=[B]) must NOT appear in C's view."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "hey B, this is private", targets=["B"])

        a_view = ctx.view_for("A")
        b_view = ctx.view_for("B")
        c_view = ctx.view_for("C")

        assert any(m["content"] == "hey B, this is private" for m in a_view)
        assert any(m["content"] == "hey B, this is private" for m in b_view)
        assert not any(m["content"] == "hey B, this is private" for m in c_view)

    def test_broadcast_visible_to_all(self, tmp_path):
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "announcement", targets=["All"])
        for name in ("A", "B", "C"):
            view = ctx.view_for(name)
            assert any(m["content"] == "announcement" for m in view), f"{name} missed broadcast"

    def test_user_message_visible_to_all(self, tmp_path):
        """User messages default to All — every agent must see them."""
        ctx = _make_context(tmp_path)
        ctx.add_message("用户", "what's the plan?")
        for name in ("A", "B", "C"):
            view = ctx.view_for(name)
            assert any(m["content"] == "what's the plan?" for m in view)

    def test_sender_always_sees_own_message(self, tmp_path):
        """Even a targeted send is visible to its sender (so the sender
        remembers what it said)."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "psst to B only", targets=["B"])
        a_view = ctx.view_for("A")
        assert any(m["content"] == "psst to B only" for m in a_view)

    def test_multi_target_message(self, tmp_path):
        """targets=[B,C] is visible to B, C, and sender A — not to others."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "hi B and C", targets=["B", "C"])
        assert any(m["content"] == "hi B and C" for m in ctx.view_for("B"))
        assert any(m["content"] == "hi B and C" for m in ctx.view_for("C"))
        assert any(m["content"] == "hi B and C" for m in ctx.view_for("A"))

    def test_empty_view_for_unknown_agent(self, tmp_path):
        """An agent that is never a target/sender sees only All-visible msgs."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "private to B", targets=["B"])
        # D is never addressed and never sends — sees nothing of the private msg
        assert not any(m["content"] == "private to B" for m in ctx.view_for("D"))


# ─────────────────────────────────────────────────────────────────────────
# Projection semantics — view is a live, non-aliased projection
# ─────────────────────────────────────────────────────────────────────────


class TestProjectionSemantics:
    def test_view_is_a_copy_not_aliased_to_log(self, tmp_path):
        """Mutating the returned list must not corrupt the log."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "msg1", targets=["All"])
        view = ctx.view_for("A")
        view.clear()  # caller mutates their copy
        # Log is unaffected — re-projecting still sees the message
        assert len(ctx.view_for("A")) == 1

    def test_view_dict_mutation_does_not_leak_into_log(self, tmp_path):
        """The returned dicts are copies — editing one can't rewrite history."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "original", targets=["All"])
        view = ctx.view_for("A")
        view[0]["content"] = "tampered"
        # Log still holds the original content
        assert ctx.messages[0]["content"] == "original"

    def test_new_log_message_appears_in_next_view(self, tmp_path):
        """A view taken before a later add must, on re-projection, see it."""
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "first", targets=["All"])
        view_before = ctx.view_for("B")
        assert len(view_before) == 1
        ctx.add_message("A", "second", targets=["All"])
        view_after = ctx.view_for("B")
        assert len(view_after) == 2
        assert any(m["content"] == "second" for m in view_after)

    def test_view_preserves_chronological_order(self, tmp_path):
        ctx = _make_context(tmp_path)
        ctx.add_message("用户", "q1")
        ctx.add_message("A", "a1", targets=["All"])
        ctx.add_message("B", "b1", targets=["All"])
        view = ctx.view_for("C")
        senders = [m["sender"] for m in view]
        assert senders == ["用户", "A", "B"]
