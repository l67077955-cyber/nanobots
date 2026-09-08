"""Cost guard mod — tier-2 soft guardrail over cumulative LLM cost.

plan-2026-09-08 batch C. Accumulates the ``cost`` field of ``llm:response``
(batch B) per agent / per session / per calendar day and, when a threshold
is crossed, (a) records + logs a warning and (b) on that agent's next
``agent:reactivated`` **appends a convergence suggestion to the tier-2
``inject`` list** — a system message the agent reads alongside the teammate
message that woke it.

Decision space (resolved from the plan's open tier-2 vs tier-3 question in
favour of tier-2): the guard is advisory only. It NEVER forces a round end,
NEVER calls engine methods, NEVER touches RoundLifecycle — the round state
machine stays the sole decider of when a round winds down (AGENTS.md #3;
pinned by tests/test_cost_guard.py with an engine write-tripwire).

Safety posture: disabled by default (opt-in via ``~/.nanobot/mods.json``),
and every limit defaults to ``None`` = no limit, so enabling the mod
without configuring thresholds changes nothing. A limit <= 0 is also
treated as unset — an explicit opt-out, never a zero budget::

    "cost_guard": {
        "enabled": true,
        "warn_ratio": 0.8,          // warn when spend >= ratio * limit
        "per_agent_limit": null,    // currency units (provider-reported)
        "per_session_limit": null,
        "daily_limit": null,
        "inject_cooldown_s": 300    // min seconds between notes per agent
    }

Known limits, deliberate: agent→session attribution uses the session seen
last on that agent's ``llm:response`` (the reactivation payload carries no
session); accounting is in-memory, so a restart resets buckets — acceptable
for a soft guardrail, the durable ledger stays in request_logs.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from nanobot.mods.base import Mod


def _limit(value: Any) -> float | None:
    """Config limit → float, or None when unset / non-positive (opt-out)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


class CostGuardMod(Mod):
    name = "cost_guard"
    version = "0.1"
    description = "成本软护栏:按 agent/会话/日累计费用,超阈值注入收敛建议(tier-2,默认关闭)"

    def default_config(self) -> dict[str, Any]:
        return {
            "warn_ratio": 0.8,
            "per_agent_limit": None,
            "per_session_limit": None,
            "daily_limit": None,
            "inject_cooldown_s": 300,
        }

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self, ctx: Any) -> None:
        self._cfg = ctx.config
        self._limit_agent = _limit(self._cfg.get("per_agent_limit"))
        self._limit_session = _limit(self._cfg.get("per_session_limit"))
        self._limit_daily = _limit(self._cfg.get("daily_limit"))
        try:
            self._warn_ratio = float(self._cfg.get("warn_ratio", 0.8))
        except (TypeError, ValueError):
            self._warn_ratio = 0.8
        try:
            self._cooldown_s = float(self._cfg.get("inject_cooldown_s", 300))
        except (TypeError, ValueError):
            self._cooldown_s = 300.0
        self.warnings: list[dict[str, Any]] = []
        self._agent_cost: dict[str, float] = {}
        self._session_cost: dict[str, float] = {}
        self._agent_session: dict[str, str] = {}
        self._day_key = time.strftime("%Y-%m-%d")
        self._day_total = 0.0
        self._warned: set[tuple[str, str]] = set()
        self._last_inject: dict[str, float] = {}

    async def stop(self) -> None:
        self._agent_cost = {}
        self._session_cost = {}
        self._agent_session = {}
        self._day_total = 0.0
        self._warned = set()
        self._last_inject = {}

    # ── Introspection (tests / future dashboard) ────────────────────────────

    def agent_cost(self, agent: str) -> float:
        return round(self._agent_cost.get(agent, 0.0), 6)

    def session_cost(self, session: str) -> float:
        return round(self._session_cost.get(session, 0.0), 6)

    def daily_cost(self) -> float:
        return round(self._day_total, 6)

    def over_budget(self, agent: str) -> bool:
        """Advisory check: agent bucket, its last-seen session, or the day
        total is over its configured limit. Pure read — no side effects."""
        if self.agent_cost(agent) > (self._limit_agent or float("inf")):
            return True
        session = self._agent_session.get(agent)
        if session and self.session_cost(session) > (self._limit_session or float("inf")):
            return True
        return self._day_total > (self._limit_daily or float("inf"))

    # ── Accounting ──────────────────────────────────────────────────────────

    def _warn(self, kind: str, bucket: str, limit: float, value: float) -> None:
        key = (bucket, kind)
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(
            {"kind": kind, "bucket": bucket, "limit": limit, "value": round(value, 6)}
        )
        logger.warning(
            "cost_guard: {} — {} spend {:.4f} / limit {:.4f}",
            "over budget" if kind == "over" else "approaching budget",
            bucket, value, limit,
        )

    def _accumulate(self, agent: Any, session: Any, cost: float) -> None:
        # day rollover resets the daily bucket
        today = time.strftime("%Y-%m-%d")
        if today != self._day_key:
            self._day_key = today
            self._day_total = 0.0

        self._day_total += cost
        if self._limit_daily:
            if self._day_total >= self._limit_daily:
                self._warn("over", "daily", self._limit_daily, self._day_total)
            elif self._day_total >= self._warn_ratio * self._limit_daily:
                self._warn("warn", "daily", self._limit_daily, self._day_total)

        if session:
            self._session_cost[str(session)] = (
                self._session_cost.get(str(session), 0.0) + cost
            )
            if self._limit_session:
                v = self._session_cost[str(session)]
                if v >= self._limit_session:
                    self._warn("over", f"session:{session}", self._limit_session, v)
                elif v >= self._warn_ratio * self._limit_session:
                    self._warn("warn", f"session:{session}", self._limit_session, v)

        if agent:
            name = str(agent)
            self._agent_cost[name] = self._agent_cost.get(name, 0.0) + cost
            if session:
                self._agent_session[name] = str(session)
            if self._limit_agent:
                v = self._agent_cost[name]
                if v >= self._limit_agent:
                    self._warn("over", f"agent:{name}", self._limit_agent, v)
                elif v >= self._warn_ratio * self._limit_agent:
                    self._warn("warn", f"agent:{name}", self._limit_agent, v)

    # ── Handlers ────────────────────────────────────────────────────────────

    async def on_llm_response(self, *, agent: Any, session: Any, cost: Any,
                              error: Any = None, **kw: Any) -> None:
        """Tier 1 (observe): accumulate cost. Failed calls carry no cost."""
        try:
            value = float(cost)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        self._accumulate(agent, session, value)

    async def on_agent_reactivated(self, *, engine: Any, agent: Any,
                                   inject: list, **kw: Any) -> None:
        """Tier 2 (filter): append a soft convergence note to ``inject``.

        ``engine`` is deliberately untouched (identity-only); the emitter
        converts appended strings into system messages for the agent.
        """
        name = str(agent or "")
        if not name or inject is None:
            return
        note = self._convergence_note(name)
        if note is None:
            return
        inject.append(note)  # append-only — tier-2 contract

    def _convergence_note(self, agent: str) -> str | None:
        """Build the note if the agent is over budget and off cooldown."""
        reasons: list[str] = []
        v = self.agent_cost(agent)
        if self._limit_agent and v >= self._limit_agent:
            reasons.append(f"该 agent 累计 {v:.2f} / 上限 {self._limit_agent:.2f}")
        session = self._agent_session.get(agent)
        if session and self._limit_session:
            sv = self.session_cost(session)
            if sv >= self._limit_session:
                reasons.append(f"会话 {session} 累计 {sv:.2f} / 上限 {self._limit_session:.2f}")
        if self._limit_daily and self._day_total >= self._limit_daily:
            reasons.append(f"今日全站累计 {self._day_total:.2f} / 上限 {self._limit_daily:.2f}")
        if not reasons:
            return None
        now = time.monotonic()
        last = self._last_inject.get(agent)
        if last is not None and (now - last) < self._cooldown_s:
            return None
        self._last_inject[agent] = now
        joined = ";".join(reasons)
        return (
            f"[cost_guard] 成本护栏:{joined}。请尽快收敛本轮讨论,"
            f"总结已有结论,避免发起需要新 LLM 调用的扩展性工作。"
        )
