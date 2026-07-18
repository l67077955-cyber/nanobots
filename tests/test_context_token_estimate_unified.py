"""Context-layer token estimate must use History.build_for_llm projection."""

from __future__ import annotations

from nanobot.core.history import History
from nanobot.groupchat.context.history_preview import estimate_history_tokens
from nanobot.groupchat.context.settings_view import estimate_history_tokens as est_view
from nanobot.utils.helpers import estimate_message_tokens


class _View:
    def __init__(self, history: History):
        self.history = history

    @property
    def active_agents(self):
        return ["Kirk"]


def test_history_estimate_includes_system_role_weight():
    h = History()
    h.system("you are a system prompt with enough text " * 5)
    h.user("hello world " * 10)
    h.agent("Kirk", "assistant reply " * 10)

    # lossy path: system → assistant, no name
    def lossy(m):
        role = "user" if m.get("sender") in ("User", "user", "用户") else "assistant"
        return estimate_message_tokens({"role": role, "content": m.get("content", "")})

    lossy_total = sum(lossy(m) for m in h.to_sender_dicts())
    good = h.estimate_tokens(estimate_message_tokens)
    # build_for_llm should not under-count vs lossy sender map in typical cases
    assert good >= lossy_total * 0.9
    assert good > 0


def test_settings_view_matches_history_estimate():
    h = History()
    h.system("sys " * 20)
    h.user("q " * 30)
    h.agent("Kirk", "a " * 30)
    v = _View(h)
    assert est_view(v) == h.estimate_tokens(estimate_message_tokens)


def test_history_preview_estimate_rehydrates_sender_dicts():
    h = History()
    h.user("hello")
    h.agent("Kirk", "hi")
    raw = h.to_sender_dicts()
    a = estimate_history_tokens(h)
    b = estimate_history_tokens(raw)
    assert a == b
    assert a > 0


def test_estimate_llm_messages_tokens_static():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    n = History.estimate_llm_messages_tokens(msgs, estimate_message_tokens)
    assert n == sum(estimate_message_tokens(m) for m in msgs)
