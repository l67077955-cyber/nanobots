"""Verify interrupt sender tracking and double-interrupt prevention."""
import sys, asyncio, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nanobot.groupchat.orchestra.mailbox import MailboxHub

async def test():
    mb = MailboxHub()
    mb.set_ranks({"kirk": "bishop", "harper": "pawn"}, leader="kirk")

    # Test 1: Low-rank sender cannot interrupt high-rank target
    mb.mark_busy("kirk")
    result = mb._try_interrupt("kirk", "harper")
    assert result is False
    assert mb._last_interrupt_sender.get("kirk") is None
    print("✅ Test1: Low-rank blocked, sender not recorded")
    mb.mark_idle("kirk")

    # Test 2: Leader can interrupt low-rank
    mb.mark_busy("harper")
    result = mb._try_interrupt("harper", "kirk")
    assert result is True
    assert mb._last_interrupt_sender.get("harper") == "kirk"
    print("✅ Test2: Leader interrupts pawn")

    # Test 3: Double-interrupt prevention (evt.is_set check)
    result = mb._try_interrupt("harper", "kirk")
    assert result is False
    assert mb._interrupt_counts.get("harper", 0) == 1
    print("✅ Test3: Double-interrupt prevented, count=1")

    # Reset for next tests
    mb.get_interrupt_event("harper").clear()
    mb.mark_idle("harper")

    # Test 4: interrupt_busy_agents with high-priority sender
    mb.mark_busy("kirk")
    mb.mark_busy("harper")
    count = mb.interrupt_busy_agents("用户")
    assert count >= 2
    print(f"✅ Test4: User interrupts all busy agents (count={count})")

    # Test 5: interrupt_busy_agents skips already-set events
    count2 = mb.interrupt_busy_agents("用户")
    assert count2 == 0
    print("✅ Test5: Skips already-set events")

    # Test 6: _last_interrupt_sender only overwritten by >= rank
    mb.get_interrupt_event("kirk").clear()
    mb.get_interrupt_event("harper").clear()
    mb._interrupt_counts.clear()
    mb._last_interrupt_sender.clear()
    mb._try_interrupt("harper", "kirk")
    assert mb._last_interrupt_sender.get("harper") == "kirk"
    mb.get_interrupt_event("harper").clear()
    mb._interrupt_counts["harper"] = 0
    mb.mark_busy("harper")
    result = mb._try_interrupt("harper", "retriever")  # retriever rank=0, harper rank=0
    assert result is False
    assert mb._last_interrupt_sender.get("harper") == "kirk"
    print("✅ Test6: Low-rank does not overwrite high-rank sender")

    print("\n🎉 All 6 tests passed")
    return True

if __name__ == "__main__":
    success = asyncio.run(test())
    sys.exit(0 if success else 1)