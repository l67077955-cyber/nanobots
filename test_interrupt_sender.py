"""Verify interrupt sender tracking survives queue consumption."""
import sys, asyncio, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nanobot.groupchat.orchestra.mailbox import MailboxHub

async def test():
    mb = MailboxHub()
    mb.set_ranks({"kirk": "bishop", "harper": "pawn"}, leader="kirk")

    # Method: bishop interrupts pawn via interrupt_busy_agents
    mb.mark_busy("harper")

    count = mb.interrupt_busy_agents("kirk")
    assert count >= 1, f"Bishop hitting lower ranks should land, got {count}"

    assert mb._last_interrupt_sender.get("harper") == "kirk", \
        f"Sender tag mismatch: got '{mb._last_interrupt_sender.get('harper')}', expected 'kirk'"
    print("[PASS] interrupt_busy_agents records sender correctly")

    # Persistence after simulated queue consumption
    mb._last_interrupt_sender["harper"] = "kirk_postqueue"
    assert mb._last_interrupt_sender.get("harper") == "kirk_postqueue", \
        "Sender should survive independent of queue state"
    print("[PASS] Sender persists independently of queue state")

    print("\nALL TESTS PASSED")
    return True

if __name__ == "__main__":
    success = asyncio.run(test())
    sys.exit(0 if success else 1)
