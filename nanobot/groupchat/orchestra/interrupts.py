"""Real-time interrupt helpers for broadcast rounds.

Handles bi-directional real-time interrupts after a successful chatroom_send:
teammate → leader (make leader aware of reports) and leader → teammate
(make teammates respond to instructions without waiting for poll).
"""

from __future__ import annotations

from loguru import logger

from nanobot.groupchat.orchestra.mailbox import MailboxHub


async def trigger_realtime_interrupts(
    sender: str,
    targets: list[str],
    mailbox: MailboxHub,
    leader_name: str | None,
) -> None:
    """Handle bi-directional real-time interrupts after a successful chatroom_send.

    Teammate -> Leader: Make leader aware of the report immediately.
    Leader -> Teammate: Make teammate respond to instructions without waiting for poll.
    """
    _targets_lower = [t.lower() for t in targets]

    # Check if the target set effectively includes someone we want to interrupt
    # (someone other than the sender).
    has_others = "all" in _targets_lower or any(t != sender.lower() for t in _targets_lower)
    if not leader_name or not has_others:
        return

    _interrupted_count = 0
    for _tgt in targets:
        if _tgt.lower() == "all":
            _interrupted_count += mailbox.interrupt_busy_agents(sender)
            break
        else:
            if mailbox._try_interrupt(_tgt, sender):
                _interrupted_count += 1

    if _interrupted_count > 0:
        _dir = "队友" if sender != leader_name else "Leader"
        _recv_str = ", ".join(targets)
        logger.info(
            "Broadcast: {} {} → {} 实时打断 {} 个 busy agent",
            _dir, sender, _recv_str, _interrupted_count,
        )
