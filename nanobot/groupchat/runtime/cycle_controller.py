"""Per-agent *branch* decisions inside the cycle loop (not busy/idle).

AgentRunner owns the only runtime lifecycle that matters: busy | idle
(plus terminal done). This module names continue/break choices after a
tool_loop result (interrupt inject, idle guard, wait, …) for testability.
It is not a multi-phase state machine and must not grow into one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CycleAction(Enum):
    """Resolved next-action for one cycle-loop decision point.

    Members are grouped by the ``decide_*`` method that emits them. The
    ``_CONTINUE`` suffix means "append the warning (if any) and re-enter
    ``tool_loop``"; ``_BREAK`` means "exit the cycle loop"; ``_FALLTHROUGH``
    means "run this body then keep evaluating the rest of the cascade (no
    continue/break)"; ``PROCEED_*`` means "none of this method's branches
    fired — run the sandwiched body and call the next ``decide_*``".
    """

    # decide_cycle_gate
    EXIT_MAX_CYCLES_FORCE_SYNTHESIS = "exit_max_cycles"          # A
    EXIT_STOPPED_OR_ENDED = "exit_stopped_or_ended"              # B
    PROCEED_TO_CYCLE = "proceed_to_cycle"                        # neither

    # decide_error_recovery
    TIMEOUT_FIRST_RETRY = "timeout_first_retry"                  # C1 (retry outcome stays body-internal)
    TIMEOUT_REPEATED_FALLTHROUGH = "timeout_repeated_fallthrough"  # C3 — NO continue
    ERROR_MAX_BREAK = "error_max_break"                          # C4
    ERROR_PLACEHOLDER_CONTINUE = "error_placeholder_continue"   # C5
    NO_ERROR_RECOVERY = "no_error_recovery"                      # not error/timeout

    # decide_post_error_guard
    INTERRUPT_CONTINUE = "interrupt_continue"                    # D
    IDLE_WARNING_CONTINUE = "idle_warning_continue"              # E  warning "idle"
    NO_TEXT_AFTER_TOOLS_CONTINUE = "no_text_after_tools_continue"  # F  warning "no_text_after_tools"
    LEADER_MGMT_NO_TEXT_CONTINUE = "leader_mgmt_no_text_continue"  # G  warning "leader_no_text_after_tools"
    PROCEED_TO_DISPLAY = "proceed_to_display"                    # none

    # decide_leader_or_single_exit
    LEADER_END_NO_TEXT_CONTINUE = "leader_end_no_text_continue"  # H1 warning "leader_end_without_text"
    LEADER_END_DISPLAY_BREAK = "leader_end_display_break"        # H2
    SINGLE_AGENT_BREAK = "single_agent_break"                    # I
    PROCEED_TO_AUTO_WAIT = "proceed_to_auto_wait"                # none

    # decide_after_wait
    WAIT_NONE_ENDED_BREAK = "wait_none_ended_break"              # J1
    WAIT_NONE_LEADER_SYNTHESIS_CONTINUE = "wait_none_leader_synth_continue"  # J2 warning "leader_wait_timeout"
    WAIT_NONE_NONLEADER_CONTINUE = "wait_none_nonleader_continue"  # J3
    WAIT_MSG_STOPPED_BREAK = "wait_msg_stopped_break"            # K
    WAIT_MSG_INJECT_CONTINUE = "wait_msg_inject_continue"        # L


@dataclass(frozen=True)
class CycleContext:
    """Immutable snapshot of the inputs a cycle-loop decision depends on.

    Built from ``_run_one`` locals at each decision point. Derived booleans
    (``is_error`` / ``is_timeout`` / ``is_interrupted`` / ``used_chatroom_send``
    / ``has_substantive_tools``) are computed inside the controller from
    ``finish_reason`` / ``tools_used`` / ``substantive_tools`` so the contract
    stays minimal and the controller is the single place that interprets them.

    ``finish_reason`` values: ``"ok"`` | ``"error"`` | ``"timeout"`` |
    ``"interrupted"`` | ``"end_discussion"`` (the last is treated as a normal
    finish — none of error/timeout/interrupted).
    """

    agent_name: str
    is_leader: bool
    cycle: int
    max_cycles: int
    total_agents: int
    engine_running: bool
    discussion_ended: bool
    leader_ended_discussion: bool
    leader_end_event_set: bool
    finish_reason: str
    content: str
    tools_used: tuple[str, ...]
    substantive_tools: frozenset[str]
    timeout_recovery_count: int
    consecutive_error_count: int
    max_consecutive_errors: int
    wait_msg: Any | None = None  # the mailbox.wait() return; sentinel for None


@dataclass(frozen=True)
class CycleDecision:
    """A controller verdict: an action + the system-warning key to append.

    ``warning_key`` is set only for the five branches that drive
    ``get_system_warning(key, name=...)`` (E/F/G/H1/J2). C2/C5 placeholder
    bodies are inline strings, not warning-key-driven, so their
    ``warning_key`` is ``None`` and their text stays body-internal.
    """

    action: CycleAction
    warning_key: str | None = None


class CycleController:
    """Concrete ``ports.CycleController`` — pure decision oracle.

    Stateless except ``agent_name`` (logging only, never affects a decision).
    Each ``decide_*`` method is a pure first-match over ``CycleContext`` that
    mirrors the exact precedence of the inline ``if``/``elif`` chain in
    ``broadcast._run_one``. Adding/moving a branch here is the single,
    test-covered place to change cycle-loop control flow.
    """

    def __init__(self, agent_name: str = "") -> None:
        self.agent_name = agent_name

    # ── derived booleans (single interpreters) ────────────────────────────

    @staticmethod
    def _is_error(ctx: CycleContext) -> bool:
        return ctx.finish_reason == "error"

    @staticmethod
    def _is_timeout(ctx: CycleContext) -> bool:
        return ctx.finish_reason == "timeout"

    @staticmethod
    def _is_interrupted(ctx: CycleContext) -> bool:
        return ctx.finish_reason == "interrupted"

    @staticmethod
    def _used_chatroom_send(ctx: CycleContext) -> bool:
        return "chatroom_send" in ctx.tools_used

    @staticmethod
    def _has_substantive(ctx: CycleContext) -> bool:
        return bool(set(ctx.tools_used) & ctx.substantive_tools)

    @staticmethod
    def _used_tools(ctx: CycleContext) -> bool:
        return bool(ctx.tools_used)

    # ── Decision 0: pre-cycle gate (A / B) ────────────────────────────────

    def decide_cycle_gate(self, ctx: CycleContext) -> CycleDecision:
        """A: ``cycle >= max_cycles`` → forced synthesis then exit.
        B: ``(not engine_running or discussion_ended) and not
        (is_leader and leader_ended_discussion)`` → exit.

        B's leader exception is load-bearing: it lets the leader retry
        synthesis after ``end_discussion`` even when ``engine._running`` is
        already False. Must preserve the exact compound condition, not
        simplify to ``engine_running``.
        """
        # A — hard cap, fires first.
        if ctx.cycle >= ctx.max_cycles:
            return CycleDecision(CycleAction.EXIT_MAX_CYCLES_FORCE_SYNTHESIS)
        # B — stop / discussion-ended, with the leader-mid-synthesis exception.
        if (
            (not ctx.engine_running or ctx.discussion_ended)
            and not (ctx.is_leader and ctx.leader_ended_discussion)
        ):
            return CycleDecision(CycleAction.EXIT_STOPPED_OR_ENDED)
        return CycleDecision(CycleAction.PROCEED_TO_CYCLE)

    # ── Decision 1: error/timeout recovery (C1–C5) ────────────────────────

    def decide_error_recovery(self, ctx: CycleContext) -> CycleDecision:
        """Resolve the error/timeout gate. C1's retry **outcome** (success
        → continue / fail → C2 placeholder → continue) stays body-internal
        because it depends on the retry ``tool_loop`` side effect; this method
        only selects the gate-level action.

        Precedence (mirrors broadcast.py L1013–1140):
          * not error/timeout          → NO_ERROR_RECOVERY
          * timeout, count == 0        → TIMEOUT_FIRST_RETRY (C1; body runs retry)
          * timeout, count != 0        → TIMEOUT_REPEATED_FALLTHROUGH (C3; no continue)
          * error, count >= MAX        → ERROR_MAX_BREAK (C4)
          * error, count <  MAX        → ERROR_PLACEHOLDER_CONTINUE (C5)
        """
        if not (self._is_error(ctx) or self._is_timeout(ctx)):
            return CycleDecision(CycleAction.NO_ERROR_RECOVERY)

        if self._is_timeout(ctx):
            # C1: first timeout gets one clean retry (body-internal outcome).
            if ctx.timeout_recovery_count == 0:
                return CycleDecision(CycleAction.TIMEOUT_FIRST_RETRY)
            # C3: repeated timeout — set error state, then FALL THROUGH (no
            # continue/break) to the history-recording body + post-error guard.
            return CycleDecision(CycleAction.TIMEOUT_REPEATED_FALLTHROUGH)

        # is_error
        if ctx.consecutive_error_count >= ctx.max_consecutive_errors:
            return CycleDecision(CycleAction.ERROR_MAX_BREAK)  # C4
        return CycleDecision(CycleAction.ERROR_PLACEHOLDER_CONTINUE)  # C5

    # ── Decision 2: post-error adequacy guard (D / E / F / G) ─────────────

    def decide_post_error_guard(self, ctx: CycleContext) -> CycleDecision:
        """First-match over D (interrupt) → E (idle cycle-1) → F (no-text
        after substantive tools) → G (leader management-only no-text) →
        proceed to display. Mirrors broadcast.py L1182–1325.

        E/F/G are an ``if``/``elif``/``elif`` chain; D is a separate ``if``
        ahead of them (interrupted finish_reason is mutually exclusive with
        the adequacy conditions, so first-match semantics hold).
        """
        # D — forced interrupt: drain queue + inject, re-enter tool_loop.
        if self._is_interrupted(ctx):
            return CycleDecision(CycleAction.INTERRUPT_CONTINUE)

        _has_content = bool(ctx.content)
        _substantive = self._has_substantive(ctx)
        _used_chatroom = self._used_chatroom_send(ctx)

        # E — anti-idle: cycle 1 produced nothing and ran no substantive tool.
        if ctx.cycle == 1 and not _has_content and not _substantive:
            return CycleDecision(CycleAction.IDLE_WARNING_CONTINUE, warning_key="idle")

        # F — ran substantive tools but produced no text (and didn't send to a
        # teammate): force a summary cycle so the output isn't swallowed.
        if not _has_content and _substantive and not _used_chatroom:
            return CycleDecision(
                CycleAction.NO_TEXT_AFTER_TOOLS_CONTINUE,
                warning_key="no_text_after_tools",
            )

        # G — leader ran manage_agent/end_discussion/transfer_credits but no
        # substantive data tool and no text: force a synthesis cycle. The
        # existing F guard above won't fire for these tool names, so without G
        # the leader silently exits without synthesis.
        if (
            ctx.is_leader
            and not _has_content
            and self._used_tools(ctx)
            and not _used_chatroom
            and not _substantive
        ):
            return CycleDecision(
                CycleAction.LEADER_MGMT_NO_TEXT_CONTINUE,
                warning_key="leader_no_text_after_tools",
            )

        return CycleDecision(CycleAction.PROCEED_TO_DISPLAY)

    # ── Decision 3: leader synthesis / single-agent exit (H / I) ──────────

    def decide_leader_or_single_exit(self, ctx: CycleContext) -> CycleDecision:
        """H: ``is_leader and leader_ended_discussion`` → H1 (no text → force
        synthesis, continue) or H2 (has text → display + break). I: single
        agent → break. Mirrors broadcast.py L1369–1414.

        H and I are separate ``if`` statements in the source, but H1 continues
        and H2 breaks, so I is only reached when H did not fire — first-match
        precedence H1 > H2 > I > auto-wait holds.
        """
        # H — leader called end_discussion this round: validate synthesis.
        if ctx.is_leader and ctx.leader_ended_discussion:
            if not ctx.content:
                # H1 — no text yet, force ONE synthesis cycle.
                return CycleDecision(
                    CycleAction.LEADER_END_NO_TEXT_CONTINUE,
                    warning_key="leader_end_without_text",
                )
            # H2 — synthesis produced: display (always, even if chatroom_send
            # was used — this is the user's only delivery channel) then exit.
            return CycleDecision(CycleAction.LEADER_END_DISPLAY_BREAK)

        # I — single agent: no teammates to wait for, exit immediately.
        if ctx.total_agents == 1:
            return CycleDecision(CycleAction.SINGLE_AGENT_BREAK)

        return CycleDecision(CycleAction.PROCEED_TO_AUTO_WAIT)

    # ── Decision 4: post-wait (J / K / L) ─────────────────────────────────

    def decide_after_wait(self, ctx: CycleContext) -> CycleDecision:
        """Resolve the action after ``mailbox.wait``. J: ``msg is None`` →
        J1 (round ended → break) / J2 (leader, no text → force synthesis) /
        J3 (non-leader → keep waiting). K: ``msg`` arrived but engine stopped
        → break. L: inject teammate message, re-enter tool_loop. Mirrors
        broadcast.py L1426–1509.
        """
        if ctx.wait_msg is None:
            # J1 — round winding down (engine stopped / leader ended /
            # discussion ended).
            if (
                not ctx.engine_running
                or ctx.leader_end_event_set
                or ctx.discussion_ended
            ):
                return CycleDecision(CycleAction.WAIT_NONE_ENDED_BREAK)
            # J2 — leader wait-timeout with no text: force synthesis.
            if ctx.is_leader and not ctx.content:
                return CycleDecision(
                    CycleAction.WAIT_NONE_LEADER_SYNTHESIS_CONTINUE,
                    warning_key="leader_wait_timeout",
                )
            # J3 — non-leader: keep waiting (stable behavior; a force-exit
            # here causes cascading stalls when other agents expect a reply).
            return CycleDecision(CycleAction.WAIT_NONE_NONLEADER_CONTINUE)

        # K — got a message, but /stop or leader-end fired while we waited.
        if not ctx.engine_running or ctx.leader_end_event_set:
            return CycleDecision(CycleAction.WAIT_MSG_STOPPED_BREAK)

        # L — inject teammate message and re-enter tool_loop.
        return CycleDecision(CycleAction.WAIT_MSG_INJECT_CONTINUE)
