"""Phase A regression tests: messages carry a ``targets`` visibility field.

Pins the contract that every message in ``HistoryContext.messages`` has a
``targets`` list, defaulting to ``["All"]`` (全员可见) when no targets are
passed.  This is the foundation for per-agent views (Phase B) — without a
targets field there is no visibility to project on.

Back-compat is the whole point of Phase A: existing call sites that pass
only ``(sender, content)`` must keep working and produce ``targets=["All"]``,
so the field is purely additive and introduces zero behavior change.  The
old tests stay green because every message is still visible to everyone.
"""

from __future__ import annotations

from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.persistence import GroupChatState


def _make_context(tmp_path) -> HistoryContext:
    """A real HistoryContext backed by an isolated GroupChatState (tmp dir).

    No session dir is created, so save_message is a no-op on disk — the test
    never touches ~/.nanobot.  Mirrors the pattern in test_crud_and_concurrency.
    """
    state = GroupChatState({"A": {}, "B": {}, "C": {}}, state_dir=tmp_path)
    return HistoryContext(state=state, provider=None)


# ─────────────────────────────────────────────────────────────────────────
# Default visibility — the no-behavior-change guarantee
# ─────────────────────────────────────────────────────────────────────────


class TestDefaultTargets:
    def test_no_targets_defaults_to_all(self, tmp_path):
        ctx = _make_context(tmp_path)
        ctx.add_message("用户", "hello")
        assert ctx.messages[-1]["targets"] == ["All"]

    def test_positional_call_still_works(self, tmp_path):
        """Old call sites use add_message(sender, content) — must not break."""
        ctx = _make_context(tmp_path)
        ctx.add_message("系统", "话题：测试")
        last = ctx.messages[-1]
        assert last["sender"] == "系统"
        assert last["targets"] == ["All"]

    def test_every_message_has_targets_field(self, tmp_path):
        ctx = _make_context(tmp_path)
        ctx.add_message("用户", "q1")
        ctx.add_message("A", "answer")
        ctx.add_message("系统", "summary")
        for m in ctx.messages:
            assert "targets" in m, f"message missing targets field: {m}"
            assert m["targets"] == ["All"]


# ─────────────────────────────────────────────────────────────────────────
# Explicit targets — the visibility primitive Phase B projects on
# ─────────────────────────────────────────────────────────────────────────


class TestExplicitTargets:
    def test_explicit_targets_preserved(self, tmp_path):
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "psst", targets=["B"])
        assert ctx.messages[-1]["targets"] == ["B"]

    def test_multiple_targets_preserved(self, tmp_path):
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "hi both", targets=["B", "C"])
        assert ctx.messages[-1]["targets"] == ["B", "C"]

    def test_explicit_all_targets(self, tmp_path):
        ctx = _make_context(tmp_path)
        ctx.add_message("A", "broadcast", targets=["All"])
        assert ctx.messages[-1]["targets"] == ["All"]

    def test_targets_not_aliased(self, tmp_path):
        """Mutating the list passed in must not leak into the stored message."""
        ctx = _make_context(tmp_path)
        incoming = ["B"]
        ctx.add_message("A", "msg", targets=incoming)
        incoming.append("C")  # caller mutates their own list afterwards
        assert ctx.messages[-1]["targets"] == ["B"]


# ─────────────────────────────────────────────────────────────────────────
# Engine passthrough — _add_message forwards targets to HistoryContext
# ─────────────────────────────────────────────────────────────────────────


class TestEngineAddMessagePassthrough:
    def test_engine_add_message_passes_targets(self, tmp_path):
        from nanobot.groupchat.orchestra.engine import GroupChatEngine

        ctx = _make_context(tmp_path)
        # Bind the real (unbound) _add_message onto a lightweight stub so we
        # exercise the actual engine code path without constructing a full
        # GroupChatEngine (which needs config/providers/MCP).
        stub = GroupChatEngine.__new__(GroupChatEngine)
        stub.history = ctx
        stub._history = ctx.messages

        stub._add_message("A", "secret", targets=["B"])
        assert ctx.messages[-1]["targets"] == ["B"]

        # Default (no targets) still routes through the engine path correctly
        stub._add_message("用户", "public")
        assert ctx.messages[-1]["targets"] == ["All"]
