"""MailboxHub.busy_agents prefers AgentRunner callback when wired."""

from __future__ import annotations

from nanobot.groupchat.orchestra.mailbox import MailboxHub


class _R:
    def __init__(self, busy: bool) -> None:
        self.is_busy = busy


def test_busy_agents_legacy_set_without_callback():
    mb = MailboxHub()
    mb.mark_busy("A")
    assert mb.busy_agents() == {"A"}
    mb.mark_idle("A")
    assert mb.busy_agents() == set()


def test_busy_agents_prefers_runner_callback():
    runners = {"A": _R(True), "B": _R(False), "C": _R(True)}
    mb = MailboxHub(get_busy_agents=lambda: {n for n, r in runners.items() if r.is_busy})
    # legacy cache may be empty or stale — callback wins
    mb.mark_busy("Z")  # should NOT appear when callback is set
    assert mb.busy_agents() == {"A", "C"}
    runners["A"].is_busy = False
    assert mb.busy_agents() == {"C"}


def test_try_interrupt_uses_live_busy():
    runners = {"Harper": _R(True)}
    mb = MailboxHub(get_busy_agents=lambda: {n for n, r in runners.items() if r.is_busy})
    mb.create("Harper")
    mb.set_ranks({"Harper": "standard", "Kirk": "advanced"})
    assert mb._try_interrupt("Harper", "Kirk") is True
    runners["Harper"].is_busy = False
    mb.get_interrupt_event("Harper").clear()
    mb._interrupt_counts.clear()
    assert mb._try_interrupt("Harper", "Kirk") is False