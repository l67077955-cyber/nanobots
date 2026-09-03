"""Behavioral tests for the mod system: discovery, lifecycle, isolation,
and the two shipped builtin mods (antirepeat migration parity + telemetry).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanobot.groupchat.orchestra.events import BroadcastEventDispatcher
from nanobot.mods.base import Mod
from nanobot.mods.builtin.antirepeat import AntiRepeatMod
from nanobot.mods.builtin.round_telemetry import RoundTelemetryMod
from nanobot.mods.manager import ModManager
from nanobot.mods.registry import discover_all, discover_builtin


class TestDiscovery:
    def test_builtin_scan_finds_shipped_mods(self):
        found = discover_builtin()
        assert "antirepeat" in found
        assert "round_telemetry" in found
        assert issubclass(found["antirepeat"], Mod)

    def test_discover_all_merges(self):
        found = discover_all()
        assert {"antirepeat", "round_telemetry"} <= set(found)


class TestManager:
    async def test_enabled_mod_subscribes_and_receives(self, tmp_path, monkeypatch):
        bus = BroadcastEventDispatcher()
        seen: list[str] = []

        class Probe(Mod):
            name = "probe"
            async def on_round_ended(self, **kw):
                seen.append("ended")

        mgr = ModManager(bus, classes={"probe": Probe})
        monkeypatch.setattr("nanobot.mods.manager._cache", {"probe": {"enabled": True}})
        started = mgr.start_all()
        await asyncio.sleep(0.02)  # let scheduled start() run
        assert started == ["probe"]
        await bus.emit("round:ended", engine=None)
        assert seen == ["ended"]
        mgr.stop_all()
        await bus.emit("round:ended", engine=None)
        assert seen == ["ended"]  # unsubscribed

    async def test_disabled_mod_not_started(self, monkeypatch):
        bus = BroadcastEventDispatcher()
        started_flag: list[str] = []

        class Probe(Mod):
            name = "probe"
            async def on_round_ended(self, **kw):
                started_flag.append("x")

        monkeypatch.setattr("nanobot.mods.manager._cache", {"probe": {"enabled": False}})
        mgr = ModManager(bus, classes={"probe": Probe})
        assert mgr.start_all() == []
        await bus.emit("round:ended", engine=None)
        assert started_flag == []

    async def test_failing_start_is_isolated(self, monkeypatch):
        bus = BroadcastEventDispatcher()

        class Bad(Mod):
            name = "bad"
            async def start(self, ctx):
                raise RuntimeError("boom")

        class Good(Mod):
            name = "good"

        monkeypatch.setattr("nanobot.mods.manager._cache",
                            {"bad": {"enabled": True}, "good": {"enabled": True}})
        mgr = ModManager(bus, classes={"bad": Bad, "good": Good})
        started = mgr.start_all()
        await asyncio.sleep(0.02)
        # bad's start() raised asynchronously — instance still registered,
        # but the good mod and the manager survive (fault containment).
        assert "good" in started
        assert mgr.active  # manager did not crash

    async def test_failing_handler_is_isolated(self, monkeypatch):
        bus = BroadcastEventDispatcher()
        hits: list[int] = []

        class Bad(Mod):
            name = "bad"
            async def on_round_ended(self, **kw):
                raise RuntimeError("boom")

        class Good(Mod):
            name = "good"
            async def on_round_ended(self, **kw):
                hits.append(1)

        monkeypatch.setattr("nanobot.mods.manager._cache",
                            {"bad": {"enabled": True}, "good": {"enabled": True}})
        mgr = ModManager(bus, classes={"bad": Bad, "good": Good})
        mgr.start_all()
        await bus.emit("round:ended", engine=None)
        assert hits == [1]


class TestAntiRepeatMigration:
    async def test_injects_when_tag_absent(self):
        mod = AntiRepeatMod()
        inject: list[str] = []
        await mod.on_agent_reactivated(
            agent="Kirk", recent_texts=["hello", "world"], inject=inject,
        )
        assert len(inject) == 1
        assert "[提醒]" in inject[0] and "Kirk" in inject[0]

    async def test_no_inject_when_recently_present(self):
        mod = AntiRepeatMod()
        inject: list[str] = []
        await mod.on_agent_reactivated(
            agent="Kirk",
            recent_texts=["[提醒] 你（Kirk）已经发表过上述观点。不要重复"],
            inject=inject,
        )
        assert inject == []

    async def test_end_to_end_via_bus(self):
        """Default-enabled wiring: emitter's inject container gets exactly the
        string the old inline code produced."""
        bus = BroadcastEventDispatcher()
        mgr = ModManager(bus, classes={"antirepeat": AntiRepeatMod})
        import nanobot.mods.manager as mm
        mm._cache = {"antirepeat": {"enabled": True}}
        try:
            mgr.start_all()
            inject: list[str] = []
            await bus.emit(
                "agent:reactivated", engine=None, agent="Harper",
                message="[Kirk]: hi", recent_texts=["old"], inject=inject,
            )
            assert inject == [
                "[提醒] 你（Harper）已经发表过上述观点。"
                "针对队友的新消息做出回应或补充新观点，不要重复已说的内容。"
            ]
        finally:
            mm._cache = None
            mgr.stop_all()


class TestRoundTelemetry:
    async def test_writes_jsonl_rows(self, tmp_path, monkeypatch):
        out = tmp_path / "tel.jsonl"
        mod = RoundTelemetryMod()
        bus = BroadcastEventDispatcher()
        mgr = ModManager(bus, classes={"round_telemetry": RoundTelemetryMod})
        monkeypatch.setattr(
            "nanobot.mods.manager._cache",
            {"round_telemetry": {"enabled": True, "out": str(out)}},
        )
        mgr.start_all()
        await asyncio.sleep(0.02)  # start() sets the path
        await bus.emit("round:started", engine=None, agents=["A", "B"], leader="A", round_num=1)
        await bus.emit("tool:result", tool="exec", ok=True, chars=42)
        mgr.stop_all()
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        by_event = {r["event"]: r for r in rows}
        assert by_event["round:started"]["leader"] == "A"
        assert by_event["tool:result"]["chars"] == 42
        assert all("ts" in r for r in rows)

    async def test_unwritable_output_never_raises(self, tmp_path, monkeypatch):
        mod = RoundTelemetryMod()
        mod._path = tmp_path / "no" / "write" / "x.jsonl"  # parent missing
        # Must swallow, not raise:
        await mod.on_round_ended(engine=None)
