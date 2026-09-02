"""Behavioral tests for RoundLifecycle — the round phase state machine.

Pins down the transitions and queries that replace the scattered flag
conjunctions (engine._running / leader_end_event / all-tasks-done), plus
the strangler-fig legacy-flag flips.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nanobot.groupchat.orchestra.round_lifecycle import RoundPhase, RoundLifecycle


def _lifecycle(**kwargs):
    return RoundLifecycle(**kwargs)


class TestTransitions:
    def test_starts_active(self):
        lc = _lifecycle()
        assert lc.phase is RoundPhase.ACTIVE
        assert lc.reason == ""
        assert lc.accepts_interjection() is True

    def test_winding_down_to_ended(self):
        lc = _lifecycle()
        lc.mark_winding_down("leader_end_discussion")
        assert lc.phase is RoundPhase.WINDING_DOWN
        assert lc.accepts_interjection() is False
        lc.mark_ended()
        assert lc.phase is RoundPhase.ENDED
        assert lc.accepts_interjection() is False

    def test_mark_winding_down_is_idempotent_and_updates_reason(self):
        lc = _lifecycle()
        lc.mark_winding_down("leader_crash")
        lc.mark_winding_down("global_timeout")
        assert lc.phase is RoundPhase.WINDING_DOWN
        assert lc.reason == "global_timeout"

    def test_ended_is_terminal(self):
        lc = _lifecycle()
        lc.mark_winding_down("leader_end_discussion")
        lc.mark_ended()
        lc.mark_winding_down("global_timeout")  # no-op
        lc.reopen()  # no-op
        assert lc.phase is RoundPhase.ENDED

    def test_reopen_returns_to_active(self):
        lc = _lifecycle()
        lc.mark_winding_down("leader_end_discussion", leader_exempt=True)
        lc.reopen()
        assert lc.phase is RoundPhase.ACTIVE
        assert lc.reason == ""
        assert lc.accepts_interjection() is True

    def test_reopen_ignored_when_active(self):
        lc = _lifecycle()
        lc.reopen()
        assert lc.phase is RoundPhase.ACTIVE


class TestLegacyFlagFlips:
    def test_mark_winding_down_sets_leader_end_event_and_running(self):
        evt = asyncio.Event()
        engine = SimpleNamespace(_running=True)
        lc = _lifecycle(leader_end_event=evt, engine=engine)
        lc.mark_winding_down("leader_end_discussion", flip_running=True)
        assert evt.is_set()
        assert engine._running is False

    def test_reopen_flips_running_back(self):
        engine = SimpleNamespace(_running=True)
        lc = _lifecycle(engine=engine)
        lc.mark_winding_down("leader_end_discussion", flip_running=True)
        assert engine._running is False
        lc.reopen()
        assert engine._running is True


class TestAgentExitQueries:
    def test_everyone_runs_while_active(self):
        lc = _lifecycle()
        assert lc.agents_should_exit(is_leader=False) is False
        assert lc.agents_should_exit(is_leader=True) is False

    def test_leader_exempt_survives_winding_down(self):
        lc = _lifecycle()
        lc.mark_winding_down("leader_end_discussion", leader_exempt=True)
        assert lc.agents_should_exit(is_leader=True) is False
        assert lc.agents_should_exit(is_leader=False) is True

    def test_no_exemption_means_everyone_exits(self):
        lc = _lifecycle()
        lc.mark_winding_down("global_timeout")
        assert lc.agents_should_exit(is_leader=True) is True
        assert lc.agents_should_exit(is_leader=False) is True

    def test_wait_should_exit_tracks_phase(self):
        lc = _lifecycle()
        assert lc.wait_should_exit() is False
        lc.mark_winding_down("converged")
        assert lc.wait_should_exit() is True
        lc.reopen()
        assert lc.wait_should_exit() is False


class TestSessionStopMapping:
    def test_stop_reasons(self):
        for reason in ("leader_end_discussion", "leader_crash", "global_timeout"):
            lc = _lifecycle()
            lc.mark_winding_down(reason)
            assert lc.session_should_stop is True, reason

    def test_converged_keeps_session_alive(self):
        lc = _lifecycle()
        lc.mark_winding_down("converged")
        assert lc.session_should_stop is False

    def test_active_round_never_stops_session(self):
        lc = _lifecycle()
        assert lc.session_should_stop is False
