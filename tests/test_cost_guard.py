"""Cost guard mod — tier-2 soft guardrail over llm:response costs.

plan-2026-09-08 batch C: per-agent / per-session / per-day cumulative cost
guard. The ONLY intervention allowed is appending a convergence suggestion
to the tier-2 ``inject`` list on ``agent:reactivated`` — never forcing a
round end, never touching engine internals or RoundLifecycle (both pinned
below). Thresholds come from mods.json; default limits are None so an
enabled-by-accident mod can never kill the production gateway.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from nanobot.groupchat.runtime.events import BroadcastEventDispatcher, set_bus
from nanobot.groupchat.runtime.round_lifecycle import RoundPhase, RoundLifecycle
from nanobot.mods.builtin.cost_guard import CostGuardMod
from nanobot.mods.manager import ModManager


@pytest.fixture
def bus():
    b = BroadcastEventDispatcher()
    set_bus(b)
    yield b
    set_bus(None)


async def _start_guard(bus, monkeypatch, cfg):
    """Start cost_guard with explicit config; returns the mod instance."""
    monkeypatch.setattr(
        "nanobot.mods.manager._cache", {"cost_guard": {"enabled": True, **cfg}}
    )
    mgr = ModManager(bus, classes={"cost_guard": CostGuardMod})
    assert mgr.start_all() == ["cost_guard"]
    await asyncio.sleep(0.02)  # manager schedules start() as a task
    return mgr, mgr._instances["cost_guard"]


async def _spend(bus, *, agent="Kirk", session="s-1", cost=1.0, n=1, model="m"):
    for _ in range(n):
        await bus.emit(
            "llm:response", agent=agent, session=session, model=model,
            input_tokens=10, output_tokens=5, cache_tokens=0,
            cost=cost, latency=0.1, error=None,
        )


async def _reactivate(bus, engine, agent="Kirk"):
    inject: list[str] = []
    await bus.emit(
        "agent:reactivated", engine=engine, agent=agent,
        message="[队友消息] ...", recent_texts=[], inject=inject,
    )
    return inject


class _EngineTripwire:
    """Any attribute write fails the test — the guard must not touch it."""

    def __setattr__(self, name, value):
        raise AssertionError(f"cost_guard must not write engine.{name}")


# ── Default safety ──────────────────────────────────────────────────────────


class TestDefaultsAreInert:
    async def test_no_limits_never_triggers(self, bus, monkeypatch):
        mgr, mod = await _start_guard(bus, monkeypatch, {})
        await _spend(bus, cost=10_000.0, n=5)
        assert mod.over_budget("Kirk") is False
        assert mod.warnings == []
        inject = await _reactivate(bus, _EngineTripwire())
        assert inject == []

    async def test_disabled_mod_subscribes_nothing(self, bus, monkeypatch):
        monkeypatch.setattr("nanobot.mods.manager._cache", {})
        mgr = ModManager(bus, classes={"cost_guard": CostGuardMod})
        assert mgr.start_all() == []
        assert bus.listener_count("llm:response") == 0
        assert bus.listener_count("agent:reactivated") == 0

    async def test_null_cost_and_errors_do_not_accumulate(self, bus, monkeypatch):
        mgr, mod = await _start_guard(bus, monkeypatch, {"per_agent_limit": 1.0})
        await bus.emit("llm:response", agent="Kirk", session="s", model="m",
                       input_tokens=None, output_tokens=None, cache_tokens=0,
                       cost=None, latency=0.2, error="HTTP 503")
        assert mod.agent_cost("Kirk") == 0.0


# ── Thresholds and warnings ────────────────────────────────────────────────


class TestThresholds:
    async def test_per_agent_over_budget_warns_and_injects(self, bus, monkeypatch):
        mgr, mod = await _start_guard(
            bus, monkeypatch, {"per_agent_limit": 5.0, "warn_ratio": 0.8}
        )
        await _spend(bus, cost=4.0)  # 80% — warn level
        assert mod.warnings and mod.warnings[0]["kind"] == "warn"
        assert mod.over_budget("Kirk") is False

        await _spend(bus, cost=1.5)  # 5.5 > 5.0 — over
        assert mod.over_budget("Kirk") is True

        inject = await _reactivate(bus, _EngineTripwire())
        assert len(inject) == 1
        assert "cost_guard" in inject[0]
        assert "5.5" in inject[0]  # carries the actual spend for actionability

    async def test_inject_cooldown_suppresses_repeat(self, bus, monkeypatch):
        mgr, mod = await _start_guard(
            bus, monkeypatch, {"per_agent_limit": 1.0, "inject_cooldown_s": 60}
        )
        await _spend(bus, cost=2.0)
        first = await _reactivate(bus, _EngineTripwire())
        second = await _reactivate(bus, _EngineTripwire())  # within cooldown
        assert len(first) == 1
        assert second == []

        mod._last_inject["Kirk"] = time.monotonic() - 61  # cooldown expired
        third = await _reactivate(bus, _EngineTripwire())
        assert len(third) == 1

    async def test_per_session_limit_tracks_sessions(self, bus, monkeypatch):
        mgr, mod = await _start_guard(bus, monkeypatch, {"per_session_limit": 3.0})
        await _spend(bus, agent="Kirk", session="s-1", cost=2.0)
        await _spend(bus, agent="Harper", session="s-1", cost=2.0)  # s-1 → 4.0
        await _spend(bus, agent="Kirk", session="s-2", cost=0.5)
        assert mod.session_cost("s-1") == 4.0
        assert mod.session_cost("s-2") == 0.5
        # session over-budget injects for agents last seen in that session
        inject = await _reactivate(bus, _EngineTripwire(), agent="Harper")
        assert len(inject) == 1

    async def test_daily_limit_buckets_by_local_date(self, bus, monkeypatch):
        mgr, mod = await _start_guard(bus, monkeypatch, {"daily_limit": 10.0})
        await _spend(bus, agent="A", session="s", cost=6.0)
        await _spend(bus, agent="B", session="s", cost=5.0)
        assert mod.daily_cost() == 11.0
        # daily is global — any agent's reactivation gets the note
        inject = await _reactivate(bus, _EngineTripwire(), agent="A")
        assert len(inject) == 1

    async def test_agent_over_budget_regardless_of_session(self, bus, monkeypatch):
        mgr, mod = await _start_guard(
            bus, monkeypatch, {"per_agent_limit": 2.0}
        )
        await _spend(bus, agent="Kirk", session="s-1", cost=1.2)
        await _spend(bus, agent="Kirk", session="s-2", cost=1.2)  # 2.4 total
        assert mod.over_budget("Kirk") is True


# ── Tier-2 compliance + core untouched ─────────────────────────────────────


class TestTier2AndCoreUntouched:
    async def test_inject_appends_never_replaces(self, bus, monkeypatch):
        mgr, mod = await _start_guard(bus, monkeypatch, {"per_agent_limit": 1.0})
        await _spend(bus, cost=2.0)
        inject: list[str] = []
        original = ["[antirepeat] avoid repeating"]
        inject.extend(original)
        await bus.emit(
            "agent:reactivated", engine=_EngineTripwire(), agent="Kirk",
            message="m", recent_texts=[], inject=inject,
        )
        assert inject[:1] == original          # existing items untouched
        assert len(inject) == 2                 # note appended, not replaced

    async def test_round_lifecycle_transitions_unaffected(self, bus, monkeypatch):
        """Over-budget guard active; the round state machine still runs its
        normal ACTIVE → WINDING_DOWN → ENDED path with no mod involvement."""
        mgr, mod = await _start_guard(bus, monkeypatch, {"per_agent_limit": 1.0})
        await _spend(bus, cost=5.0)
        assert mod.over_budget("Kirk") is True

        inject = await _reactivate(bus, _EngineTripwire())
        assert len(inject) == 1  # guard did fire

        lc = RoundLifecycle(engine=SimpleNamespace(_running=True))
        assert lc.phase is RoundPhase.ACTIVE
        lc.mark_winding_down("leader_end_discussion")
        assert lc.phase is RoundPhase.WINDING_DOWN
        assert lc.accepts_interjection() is False
        lc.mark_ended()
        assert lc.phase is RoundPhase.ENDED
        # guard state survives the round ending (daily accounting persists)
        assert mod.over_budget("Kirk") is True

    async def test_no_round_ending_events_emitted_by_guard(self, bus, monkeypatch):
        seen: list[str] = []
        for ev in ("round:winding_down", "round:ended", "round:reopened"):
            async def _spy(_ev=ev, **kw):
                seen.append(_ev)
            bus.on(ev, _spy)
        mgr, mod = await _start_guard(bus, monkeypatch, {"per_agent_limit": 1.0})
        await _spend(bus, cost=5.0)
        await _reactivate(bus, _EngineTripwire())
        assert seen == []  # soft guardrail only — the engine stays the decider
