"""Interrupt hierarchy: higher configured tier preempts lower tier."""

from nanobot.groupchat.runtime.mailbox import MailboxHub


def test_advanced_interrupts_standard_leader():
    """Kirk(advanced) must preempt Harper(standard) even when Harper is Leader."""
    hub = MailboxHub(["Harper", "Kirk"])
    hub.set_ranks({"Harper": "standard", "Kirk": "advanced"}, leader="Harper")
    hub.mark_busy("Harper")

    assert hub._tier_rank("Kirk") == 2
    assert hub._tier_rank("Harper") == 1
    assert hub._can_interrupt("Kirk", "Harper") is True
    assert hub._try_interrupt("Harper", "Kirk") is True
    assert hub.get_interrupt_event("Harper").is_set()


def test_standard_cannot_interrupt_advanced():
    hub = MailboxHub(["A", "B"])
    hub.set_ranks({"A": "standard", "B": "advanced"}, leader="")
    hub.mark_busy("B")

    assert hub._can_interrupt("A", "B") is False
    assert hub._try_interrupt("B", "A") is False


def test_equal_tier_peers_cannot_interrupt():
    hub = MailboxHub(["A", "B"])
    hub.set_ranks({"A": "standard", "B": "standard"}, leader="")
    hub.mark_busy("B")

    assert hub._can_interrupt("A", "B") is False


def test_leader_sender_still_interrupts_anyone():
    hub = MailboxHub(["Harper", "Kirk"])
    hub.set_ranks({"Harper": "standard", "Kirk": "advanced"}, leader="Harper")
    hub.mark_busy("Kirk")

    assert hub._can_interrupt("Harper", "Kirk") is True
    assert hub._try_interrupt("Kirk", "Harper") is True