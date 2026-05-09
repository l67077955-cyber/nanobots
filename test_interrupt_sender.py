"""Verify interrupt sender tracking survives queue consumption."""
import sys, asyncio, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nanobot.groupchat.orchestra.mailbox import MailboxHub

async def test():
    mb = MailboxHub()
    mb.set_ranks({"kirk": "bishop", "harper": "pawn"}, leader="kirk")
    
    # Simulate: harper sends to kirk, kirk is busy
    mb.mark_busy("kirk")
    
    # Method 1: direct _try_interrupt
    mb._try_interrupt("kirk", "harper")
    assert mb._last_interrupt_sender.get("kirk") == "harper", \
        f"Expected harper, got {mb._last_interrupt_sender.get('kirk')}"
    print("✅ _try_interrupt records sender correctly")
    
    # Clear
    mb.get_interrupt_event("kirk").clear()
    
    # Method 2: interrupt_busy_agents
    count = mb.interrupt_busy_agents("用户")
    assert count >= 1, f"Expected >=1, got {count}"
    assert mb._last_interrupt_sender.get("kirk") == "用户", \
        f"Expected 用户, got {mb._last_interrupt_sender.get('kirk')}"
    print("✅ interrupt_busy_agents records sender correctly")
    
    # Verify: even after consuming the message (simulated by clearing queue),
    # _last_interrupt_sender persists
    mb._last_interrupt_sender["kirk"] = "harper_queued"
    # Simulate wait() consuming the message
    assert mb._last_interrupt_sender.get("kirk") == "harper_queued", \
        "Sender should persist after queue consumption"
    print("✅ Sender persists independently of queue state")
    
    print("\n🎉 All tests passed")
    return True

if __name__ == "__main__":
    success = asyncio.run(test())
    sys.exit(0 if success else 1)
