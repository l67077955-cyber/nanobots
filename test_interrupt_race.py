"""Reproduction test: verify interrupt behavior across all agent states."""
import sys, asyncio, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nanobot.groupchat.orchestra.mailbox import MailboxHub

def setup_mb(leader="kirk"):
    mb = MailboxHub()
    mb.set_ranks({"kirk": "bishop", "harper": "knight", "lucas": "pawn"}, leader=leader)
    for name in ["kirk", "harper", "lucas"]:
        mb.create(name)
    return mb

async def test_interrupt_when_busy():
    """Target is in tool_loop (busy) -> interrupt should fire."""
    mb = setup_mb()
    mb.mark_busy("harper")
    evt = mb.get_interrupt_event("harper")

    delivered = mb.send("kirk", ["All"], "urgent message")
    assert delivered == 2, f"Expected 2 delivered, got {delivered}"
    assert evt.is_set(), "FAIL: Harper's interrupt event NOT set despite being busy!"
    assert mb._last_interrupt_sender.get("harper") == "kirk"
    print("[PASS] test_interrupt_when_busy")

async def test_interrupt_when_idle():
    """Target in auto-wait (idle) -> no interrupt, but message in queue."""
    mb = setup_mb()
    evt = mb.get_interrupt_event("harper")

    delivered = mb.send("kirk", ["All"], "urgent message")
    assert delivered == 2
    assert not evt.is_set(), "Event should NOT be set for idle agent"

    msg = mb._queues["harper"].get_nowait()
    assert msg.sender == "kirk"
    print("[PASS] test_interrupt_when_idle")

async def test_interrupt_count_limit():
    """After 3 interrupts per round, further are skipped."""
    mb = setup_mb()
    mb.mark_busy("harper")
    evt = mb.get_interrupt_event("harper")

    for i in range(3):
        evt.clear()
        result = mb._try_interrupt("harper", "kirk")
        assert result, f"interrupt #{i+1} should succeed"

    evt.clear()
    result = mb._try_interrupt("harper", "kirk")
    assert not result, "4th interrupt should be skipped"
    print("[PASS] test_interrupt_count_limit")

async def test_interrupt_busy_agents_respects_rank():
    """interrupt_busy_agents() now respects rank + blocks self + blocks equal-rank."""
    # --- Case A: low rank (pawn) cannot interrupt higher rank (knight) ---
    mb = setup_mb()
    mb.mark_busy("harper")   # knight

    count = mb.interrupt_busy_agents("lucas")  # pawn tries to interrupt knights+bishops
    assert count == 0, f"Pawn interrupting knight should yield 0, got {count}"

    evt_knight = mb.get_interrupt_event("harper")
    assert not evt_knight.is_set(), \
        "Knight's event should NOT be set by pawn-ranked lucas"

    # --- Case B: Self-interrupt blocked ---
    mb.mark_busy("lucas")

    count = mb.interrupt_busy_agents("lucas")
    assert count == 0, f"Self-interrupt should yield 0, got {count}"

    own_evt = mb.get_interrupt_event("lucas")
    assert not own_evt.is_set(), \
        "Own event should never be set by self"

    # --- Case C: High rank CAN interrupt lower rank ---
    evt_knight.clear()

    count = mb.interrupt_busy_agents("kirk")  # bishop interrupts everyone below
    # Should hit harper(knight) and lucas(pawn) — both lower than bishop
    assert count == 2, f"Bishop interrupting all lower ranks should yield 2, got {count}"

    print("[PASS] test_interrupt_busy_agents_respects_rank")

async def main():
	tests=[
		test_interrupt_when_busy,
		test_interrupt_when_idle,
		test_interrupt_count_limit,
		test_interrupt_busy_agents_respects_rank,
	]
	passed=failed=0
	for t in tests:
		try:
			await t()
			passed+=1
		except AssertionError as e:
			failed+=1;print(f"[FAIL]{t.__name__}:{e}")
		except Exception as e:
			failed+=1;print(f"[ERR]{t.__name__}:{type(e).__name__}:{e}")
	print(f"\n{'='*60}")
	print(f"Results:{passed}/{len(tests)}passed{failed}failed")
	return failed==0

if __name__=="__main__":
	success=asyncio.run(main())
	sys.exit(0 if success else 1)
