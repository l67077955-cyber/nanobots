"""RoundResult contract: broadcast_round reports the session verdict explicitly.

Phase 1 step 3 of docs/plan-2026-09-07-arch-refactor.md retires the
``flip_running`` side channel (round-level code writing ``engine._running``)
and instead returns ``lifecycle.session_should_stop`` to the caller. These
tests pin the new contract at the real ``broadcast_round`` boundary:

- the return value is a ``RoundResult`` carrying ``messages`` plus
  ``session_should_stop`` (not a bare list);
- a round that ends via global timeout reports ``session_should_stop=True``
  **without** flipping the session-level ``engine._running`` flag — the
  caller (run_loop) decides what to do with the verdict.

The timeout scenario runs the REAL ``broadcast_round`` with a minimal
duck-typed engine (same "real function + tiny fake" altitude as
tests/test_user_ingress.py): the round's single agent is absent from
``engine.registry`` so its task completes instantly (broadcast.py early
return), the auxiliary listeners/sentinels never fire, and the tiny
``global_timeout`` drives the round into the global-timeout branch.
"""

from __future__ import annotations

import asyncio

import pytest

from nanobot.groupchat.runtime.broadcast import RoundResult, broadcast_round
from nanobot.groupchat.runtime.mailbox import MailboxHub
from nanobot.tools.registry import ToolRegistry


class _FakeHistory:
    def all_messages(self):
        return []


class _FakeEngine:
    """Duck-typed engine surface actually touched by broadcast_round."""

    def __init__(self):
        # "Ghost" is NOT in the registry → its agent task completes instantly.
        self.registry = {"Someone-Else": {"model": "m"}}
        self._active_agents = ["Ghost"]
        self._round = 0
        self._running = True
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending_join_queue: asyncio.Queue[str] = asyncio.Queue()
        self._broadcast_tasks: dict[str, asyncio.Task] = {}
        self.history = _FakeHistory()
        self.sent: list[str] = []
        self.round_summaries: list[dict] = []

    async def _send(self, text: str) -> None:
        self.sent.append(text)

    def _add_message(self, sender: str, content: str) -> None:
        pass

    def _save_event(self, event, extra=None, **kwargs) -> None:
        pass

    def _save_round_summary(self, **kwargs) -> None:
        self.round_summaries.append(kwargs)

    def _get_agent_registry(self, name: str):
        return ToolRegistry()


@pytest.mark.asyncio
async def test_empty_round_returns_round_result():
    """No agents → still a RoundResult, session verdict False."""
    result = await broadcast_round([], _FakeEngine(), MailboxHub())
    assert isinstance(result, RoundResult)
    assert result.messages == []
    assert result.session_should_stop is False


@pytest.mark.asyncio
async def test_global_timeout_reports_session_stop_without_flag_side_effect():
    """A global-timeout round must report its verdict, not write the flag.

    Previously the timeout branch called
    ``mark_winding_down("global_timeout", flip_running=True)`` which set
    ``engine._running = False`` behind the caller's back. Now the verdict
    travels through the return value only.
    """
    engine = _FakeEngine()
    mailbox = MailboxHub()

    result = await broadcast_round(["Ghost"], engine, mailbox, global_timeout=0.05)

    assert isinstance(result, RoundResult)
    # Ghost's task completes instantly (not in registry) and is collected;
    # the round then dies of global timeout → session verdict True.
    assert result.session_should_stop is True
    assert result.messages == [("Ghost", None)]
    # Round-level code must NOT write the session-level flag: run_loop owns it.
    assert engine._running is True
