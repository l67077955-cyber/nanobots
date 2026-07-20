"""Unit tests for CycleController — the cycle-loop decision oracle (Step 3b).

Pure tests: construct a frozen ``CycleContext``, call a ``decide_*`` method,
assert on ``CycleDecision.action`` + ``warning_key``. No engine/mailbox/async.
Covers every branch of the five decision methods plus the edge-case
interactions the refactor's risk audit named: B-exception (leader
mid-synthesis), A-vs-H precedence, C3 fall-through, C3→H1 reachability,
F-vs-G precedence, J1/J2/J3 three-way first-match, E cycle-1 gating.

The body-internal C1 retry outcome (success → continue / fail → C2
placeholder → continue) is NOT a pure decision and stays in ``_run_one``; it
is exercised by the integration harness ``test_broadcast_run_one_cycle.py``,
not here.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.groupchat.runtime.cycle_controller import (
    CycleAction,
    CycleContext,
    CycleController,
)

_SUBSTANTIVE = frozenset({"web_search", "web_fetch", "exec", "read_file", "write_file"})


def _ctx(**over: Any) -> CycleContext:
    """Baseline: non-leader, mid-round, ok finish, with content + chatroom_send."""
    base = dict(
        agent_name="Kirk",
        is_leader=False,
        cycle=2,
        max_cycles=20,
        total_agents=3,
        engine_running=True,
        discussion_ended=False,
        leader_ended_discussion=False,
        leader_end_event_set=False,
        finish_reason="ok",
        content="hello",
        tools_used=("chatroom_send",),
        substantive_tools=_SUBSTANTIVE,
        timeout_recovery_count=0,
        consecutive_error_count=0,
        max_consecutive_errors=3,
        total_timeout_count=0,
        max_timeout_recoveries=3,
        wait_msg=None,
    )
    base.update(over)
    return CycleContext(**base)


_CTRL = CycleController("Kirk")


# ── decide_cycle_gate: A / B ───────────────────────────────────────────────


class TestCycleGate:
    def test_a_max_cycles(self):
        d = _CTRL.decide_cycle_gate(_ctx(cycle=20, max_cycles=20))
        assert d.action is CycleAction.EXIT_MAX_CYCLES_FORCE_SYNTHESIS

    def test_a_precedence_over_b(self):
        # Both A and B would fire; A wins (max_cycles is the hard backstop).
        d = _CTRL.decide_cycle_gate(_ctx(cycle=20, max_cycles=20, engine_running=False))
        assert d.action is CycleAction.EXIT_MAX_CYCLES_FORCE_SYNTHESIS

    def test_b_engine_stopped(self):
        d = _CTRL.decide_cycle_gate(_ctx(engine_running=False))
        assert d.action is CycleAction.EXIT_STOPPED_OR_ENDED

    def test_b_discussion_ended(self):
        d = _CTRL.decide_cycle_gate(_ctx(discussion_ended=True))
        assert d.action is CycleAction.EXIT_STOPPED_OR_ENDED

    def test_b_exception_leader_mid_synthesis(self):
        # engine stopped BUT leader is mid-synthesis → must PROCEED, not exit.
        d = _CTRL.decide_cycle_gate(
            _ctx(engine_running=False, is_leader=True, leader_ended_discussion=True)
        )
        assert d.action is CycleAction.PROCEED_TO_CYCLE

    def test_b_exception_only_for_leader(self):
        # Same condition but non-leader → still exits (exception is leader-only).
        d = _CTRL.decide_cycle_gate(
            _ctx(engine_running=False, is_leader=False, leader_ended_discussion=True)
        )
        assert d.action is CycleAction.EXIT_STOPPED_OR_ENDED

    def test_b_exception_requires_leader_ended(self):
        # Leader, engine stopped, but hasn't called end_discussion → exit.
        d = _CTRL.decide_cycle_gate(
            _ctx(engine_running=False, is_leader=True, leader_ended_discussion=False)
        )
        assert d.action is CycleAction.EXIT_STOPPED_OR_ENDED

    def test_normal_proceeds(self):
        assert _CTRL.decide_cycle_gate(_ctx()).action is CycleAction.PROCEED_TO_CYCLE


# ── decide_error_recovery: C1 / C3 / C4 / C5 ───────────────────────────────


class TestErrorRecovery:
    def test_no_error(self):
        assert _CTRL.decide_error_recovery(_ctx()).action is CycleAction.NO_ERROR_RECOVERY

    def test_c0_circuit_break_at_max(self):
        # Cumulative timeouts hit the cap → circuit break, even when the
        # per-streak recovery counter says this is a "first" timeout (the
        # placeholder path resets it every round).
        d = _CTRL.decide_error_recovery(
            _ctx(finish_reason="timeout", timeout_recovery_count=0, total_timeout_count=3)
        )
        assert d.action is CycleAction.TIMEOUT_CIRCUIT_BREAK
        assert d.warning_key is None

    def test_c0_circuit_break_overrides_fallthrough(self):
        d = _CTRL.decide_error_recovery(
            _ctx(finish_reason="timeout", timeout_recovery_count=1, total_timeout_count=5)
        )
        assert d.action is CycleAction.TIMEOUT_CIRCUIT_BREAK

    def test_c0_not_below_max(self):
        d = _CTRL.decide_error_recovery(
            _ctx(finish_reason="timeout", timeout_recovery_count=0, total_timeout_count=2)
        )
        assert d.action is CycleAction.TIMEOUT_FIRST_RETRY

    def test_c1_first_timeout(self):
        d = _CTRL.decide_error_recovery(_ctx(finish_reason="timeout", timeout_recovery_count=0))
        assert d.action is CycleAction.TIMEOUT_FIRST_RETRY
        assert d.warning_key is None  # retry outcome stays body-internal

    def test_c3_repeated_timeout_fallthrough(self):
        d = _CTRL.decide_error_recovery(_ctx(finish_reason="timeout", timeout_recovery_count=1))
        assert d.action is CycleAction.TIMEOUT_REPEATED_FALLTHROUGH
        assert d.warning_key is None

    def test_c3_not_when_count_zero(self):
        d = _CTRL.decide_error_recovery(_ctx(finish_reason="timeout", timeout_recovery_count=0))
        assert d.action is CycleAction.TIMEOUT_FIRST_RETRY  # not fallthrough

    def test_c4_consecutive_errors_max(self):
        d = _CTRL.decide_error_recovery(
            _ctx(finish_reason="error", consecutive_error_count=3, max_consecutive_errors=3)
        )
        assert d.action is CycleAction.ERROR_MAX_BREAK

    def test_c4_boundary_below_max(self):
        # count=2 < MAX=3 → not C4, falls to C5.
        d = _CTRL.decide_error_recovery(
            _ctx(finish_reason="error", consecutive_error_count=2, max_consecutive_errors=3)
        )
        assert d.action is CycleAction.ERROR_PLACEHOLDER_CONTINUE

    def test_c5_error_placeholder(self):
        d = _CTRL.decide_error_recovery(
            _ctx(finish_reason="error", consecutive_error_count=0, max_consecutive_errors=3)
        )
        assert d.action is CycleAction.ERROR_PLACEHOLDER_CONTINUE
        assert d.warning_key is None  # placeholder text is body-internal


# ── decide_post_error_guard: D / E / F / G ─────────────────────────────────


class TestPostErrorGuard:
    def test_d_interrupt(self):
        d = _CTRL.decide_post_error_guard(_ctx(finish_reason="interrupted"))
        assert d.action is CycleAction.INTERRUPT_CONTINUE

    def test_d_precedence_over_e(self):
        # Interrupted + cycle 1 + no content + no tools → D wins, not E.
        d = _CTRL.decide_post_error_guard(
            _ctx(finish_reason="interrupted", cycle=1, content="", tools_used=())
        )
        assert d.action is CycleAction.INTERRUPT_CONTINUE

    def test_e_idle_cycle1(self):
        d = _CTRL.decide_post_error_guard(
            _ctx(cycle=1, content="", tools_used=())
        )
        assert d.action is CycleAction.IDLE_WARNING_CONTINUE
        assert d.warning_key == "idle"

    def test_e_only_on_cycle1(self):
        # Identical state at cycle 2 → E does NOT fire (no substantive, no
        # chatroom_send, no content → falls through to PROCEED_TO_DISPLAY).
        d = _CTRL.decide_post_error_guard(_ctx(cycle=2, content="", tools_used=()))
        assert d.action is CycleAction.PROCEED_TO_DISPLAY

    def test_f_no_text_after_tools(self):
        d = _CTRL.decide_post_error_guard(
            _ctx(content="", tools_used=("web_search",))
        )
        assert d.action is CycleAction.NO_TEXT_AFTER_TOOLS_CONTINUE
        assert d.warning_key == "no_text_after_tools"

    def test_f_skipped_when_chatroom_send_used(self):
        # chatroom_send already delivered the text → no forced summary.
        d = _CTRL.decide_post_error_guard(
            _ctx(content="", tools_used=("web_search", "chatroom_send"))
        )
        assert d.action is CycleAction.PROCEED_TO_DISPLAY

    def test_f_vs_g_precedence(self):
        # Leader, substantive tool, no content, no chatroom_send → F (not G),
        # because F's elif precedes G and substantive is present.
        d = _CTRL.decide_post_error_guard(
            _ctx(is_leader=True, content="", tools_used=("web_search",))
        )
        assert d.action is CycleAction.NO_TEXT_AFTER_TOOLS_CONTINUE

    def test_g_leader_mgmt_no_text(self):
        d = _CTRL.decide_post_error_guard(
            _ctx(is_leader=True, content="", tools_used=("manage_agent",))
        )
        assert d.action is CycleAction.LEADER_MGMT_NO_TEXT_CONTINUE
        assert d.warning_key == "leader_no_text_after_tools"

    def test_g_excluded_when_substantive_present(self):
        # manage_agent + web_search → substantive present → G's gate fails → F.
        d = _CTRL.decide_post_error_guard(
            _ctx(is_leader=True, content="", tools_used=("manage_agent", "web_search"))
        )
        assert d.action is CycleAction.NO_TEXT_AFTER_TOOLS_CONTINUE

    def test_normal_with_content_proceeds(self):
        assert _CTRL.decide_post_error_guard(_ctx(content="hi")).action is CycleAction.PROCEED_TO_DISPLAY

    def test_content_skips_all_guards(self):
        # cycle 1 with content + no tools → PROCEED (E/F/G all need no content).
        d = _CTRL.decide_post_error_guard(_ctx(cycle=1, content="hi", tools_used=()))
        assert d.action is CycleAction.PROCEED_TO_DISPLAY


# ── decide_leader_or_single_exit: H1 / H2 / I ──────────────────────────────


class TestLeaderOrSingleExit:
    def test_h1_no_text(self):
        d = _CTRL.decide_leader_or_single_exit(
            _ctx(is_leader=True, leader_ended_discussion=True, content="")
        )
        assert d.action is CycleAction.LEADER_END_NO_TEXT_CONTINUE
        assert d.warning_key == "leader_end_without_text"

    def test_h2_has_text(self):
        d = _CTRL.decide_leader_or_single_exit(
            _ctx(is_leader=True, leader_ended_discussion=True, content="synthesis")
        )
        assert d.action is CycleAction.LEADER_END_DISPLAY_BREAK
        assert d.warning_key is None

    def test_h1_below_cap_still_forces(self):
        # One retry consumed, cap is 2 → still force a synthesis cycle.
        d = _CTRL.decide_leader_or_single_exit(
            _ctx(is_leader=True, leader_ended_discussion=True, content="",
                 leader_end_no_text_retries=1, max_leader_end_synthesis_retries=2)
        )
        assert d.action is CycleAction.LEADER_END_NO_TEXT_CONTINUE
        assert d.warning_key == "leader_end_without_text"

    def test_h1_capped_after_max_retries(self):
        # H1 is bounded: retries exhausted → exit instead of looping forever
        # (2026-07-19 end-of-discussion stall).
        d = _CTRL.decide_leader_or_single_exit(
            _ctx(is_leader=True, leader_ended_discussion=True, content="",
                 leader_end_no_text_retries=2, max_leader_end_synthesis_retries=2)
        )
        assert d.action is CycleAction.LEADER_END_DISPLAY_BREAK
        assert d.warning_key is None

    def test_h_only_when_leader_ended(self):
        # Leader, no content, but hasn't ended discussion → not H; total=3 → auto-wait.
        d = _CTRL.decide_leader_or_single_exit(
            _ctx(is_leader=True, leader_ended_discussion=False, content="")
        )
        assert d.action is CycleAction.PROCEED_TO_AUTO_WAIT

    def test_i_single_agent(self):
        d = _CTRL.decide_leader_or_single_exit(_ctx(total_agents=1, content="hi"))
        assert d.action is CycleAction.SINGLE_AGENT_BREAK

    def test_i_not_reached_when_h2_fires(self):
        # Single agent + leader ended + has text → H2 wins (break), I not reached.
        d = _CTRL.decide_leader_or_single_exit(
            _ctx(is_leader=True, leader_ended_discussion=True, total_agents=1, content="x")
        )
        assert d.action is CycleAction.LEADER_END_DISPLAY_BREAK

    def test_normal_proceeds_to_auto_wait(self):
        assert _CTRL.decide_leader_or_single_exit(_ctx()).action is CycleAction.PROCEED_TO_AUTO_WAIT


# ── decide_after_wait: J1 / J2 / J3 / K / L ────────────────────────────────


class TestAfterWait:
    def test_j1_engine_stopped(self):
        d = _CTRL.decide_after_wait(_ctx(wait_msg=None, engine_running=False))
        assert d.action is CycleAction.WAIT_NONE_ENDED_BREAK

    def test_j1_leader_end_event(self):
        d = _CTRL.decide_after_wait(_ctx(wait_msg=None, leader_end_event_set=True))
        assert d.action is CycleAction.WAIT_NONE_ENDED_BREAK

    def test_j1_discussion_ended(self):
        d = _CTRL.decide_after_wait(_ctx(wait_msg=None, discussion_ended=True))
        assert d.action is CycleAction.WAIT_NONE_ENDED_BREAK

    def test_j2_leader_no_text(self):
        d = _CTRL.decide_after_wait(
            _ctx(wait_msg=None, is_leader=True, content="")
        )
        assert d.action is CycleAction.WAIT_NONE_LEADER_SYNTHESIS_CONTINUE
        assert d.warning_key == "leader_wait_timeout"

    def test_j2_skipped_when_leader_has_text(self):
        d = _CTRL.decide_after_wait(
            _ctx(wait_msg=None, is_leader=True, content="x")
        )
        assert d.action is CycleAction.WAIT_NONE_NONLEADER_CONTINUE

    def test_j2_skipped_for_non_leader(self):
        # Non-leader, no text, wait timeout → J3 (not J2).
        d = _CTRL.decide_after_wait(_ctx(wait_msg=None, is_leader=False, content=""))
        assert d.action is CycleAction.WAIT_NONE_NONLEADER_CONTINUE

    def test_j3_non_leader_keep_waiting(self):
        d = _CTRL.decide_after_wait(_ctx(wait_msg=None, is_leader=False))
        assert d.action is CycleAction.WAIT_NONE_NONLEADER_CONTINUE

    def test_k_stopped_after_wait(self):
        d = _CTRL.decide_after_wait(_ctx(wait_msg="msg", engine_running=False))
        assert d.action is CycleAction.WAIT_MSG_STOPPED_BREAK

    def test_k_leader_end_after_wait(self):
        d = _CTRL.decide_after_wait(_ctx(wait_msg="msg", leader_end_event_set=True))
        assert d.action is CycleAction.WAIT_MSG_STOPPED_BREAK

    def test_l_inject_teammate_msg(self):
        d = _CTRL.decide_after_wait(_ctx(wait_msg="msg"))
        assert d.action is CycleAction.WAIT_MSG_INJECT_CONTINUE


# ── Cross-method edge-case interactions (the risk-audit targets) ──────────


class TestInteractions:
    def test_a_beats_h_when_max_cycles_and_leader_ended(self):
        # cycle>=max + leader + leader_ended + no content: the gate returns A
        # (forced synthesis+exit) — H1 is never consulted because the gate
        # fires first. Confirms A precedence over H within the cascade.
        ctx = _ctx(
            cycle=30, max_cycles=30, is_leader=True,
            leader_ended_discussion=True, content="",
        )
        assert _CTRL.decide_cycle_gate(ctx).action is CycleAction.EXIT_MAX_CYCLES_FORCE_SYNTHESIS

    def test_c3_then_h1_is_reachable(self):
        # The C3 fall-through path must still allow H1 to fire afterwards.
        # Step 1: error recovery on a repeated-timeout ctx → fall-through.
        c3_ctx = _ctx(
            finish_reason="timeout", timeout_recovery_count=1,
            is_leader=True, leader_ended_discussion=True, content="",
        )
        assert _CTRL.decide_error_recovery(c3_ctx).action is CycleAction.TIMEOUT_REPEATED_FALLTHROUGH
        # Step 2 (after the history body + post-error guard run inline): the
        # leader/single-exit decision on the same state → H1 synthesis retry.
        assert _CTRL.decide_leader_or_single_exit(c3_ctx).action is CycleAction.LEADER_END_NO_TEXT_CONTINUE

    def test_c3_then_post_error_guard_proceeds(self):
        # After C3 fall-through, post-error guard on empty no-tool state →
        # PROCEED_TO_DISPLAY (display then skipped for empty content).
        c3_ctx = _ctx(
            finish_reason="timeout", timeout_recovery_count=1,
            content="", tools_used=(),
        )
        assert _CTRL.decide_error_recovery(c3_ctx).action is CycleAction.TIMEOUT_REPEATED_FALLTHROUGH
        assert _CTRL.decide_post_error_guard(c3_ctx).action is CycleAction.PROCEED_TO_DISPLAY

    def test_j_three_way_first_match(self):
        # J1 > J2 > J3 ordering on wait_msg=None.
        base = dict(wait_msg=None)
        # J1 fires whenever the round is winding down, regardless of leader/text.
        assert _CTRL.decide_after_wait(
            _ctx(**base, engine_running=False, is_leader=True, content="")
        ).action is CycleAction.WAIT_NONE_ENDED_BREAK
        # J2: not winding down, leader, no text.
        assert _CTRL.decide_after_wait(
            _ctx(**base, is_leader=True, content="")
        ).action is CycleAction.WAIT_NONE_LEADER_SYNTHESIS_CONTINUE
        # J3: not winding down, non-leader (or leader with text).
        assert _CTRL.decide_after_wait(
            _ctx(**base, is_leader=False, content="")
        ).action is CycleAction.WAIT_NONE_NONLEADER_CONTINUE

    def test_k_beats_l_when_stopped(self):
        # msg arrived but engine stopped → K break, not L inject.
        ctx = _ctx(wait_msg="msg", engine_running=False)
        assert _CTRL.decide_after_wait(ctx).action is CycleAction.WAIT_MSG_STOPPED_BREAK

    def test_dead_state_not_in_context(self):
        # _leader_disabled_agent is dead state and must NOT be a CycleContext
        # field (construction succeeds without it; it has no such parameter).
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(CycleContext)}
        assert "leader_disabled_agent" not in field_names
        assert "tool_calls_detail" not in field_names
