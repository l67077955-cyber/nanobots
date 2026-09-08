"""Otel export mod — span tree construction + off-by-default guarantees.

plan-2026-09-08 batch B step 2: pin the opt-in OTLP export mod.

- Span tree: workflow (round:started→round:ended) → agent (first agent
  event → agent:done) → model (llm:response, backdated by latency) and
  tool spans, using the real OTel SDK with the in-memory span exporter.
- gen_ai.* semconv names exist ONLY inside the mod's export layer — no
  internal event payload ever carries them.
- Disabled mod = zero bus subscriptions, zero behaviour. Module import
  never touches the opentelemetry SDK (discovery imports builtin modules
  unconditionally; machines without the otel extra must not break).

Local-verification limit (honest scope): a real OTLP collector is not
runnable on this machine — end-to-end OTLP delivery is NOT claimed here.
These tests stop at exporter wiring + span construction.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.groupchat.runtime.events import BroadcastEventDispatcher, set_bus
from nanobot.mods.builtin.otel_export import OtelExportMod
from nanobot.mods.manager import ModManager

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bus():
    b = BroadcastEventDispatcher()
    set_bus(b)
    yield b
    set_bus(None)


async def _start_mod(bus, monkeypatch, *, extra_cfg=None):
    """Start otel_export against *bus* with an in-memory span exporter.

    Returns (manager, mod_instance). _make_exporter is the mod's only
    network seam; overriding it swaps OTLP for the SDK's in-memory
    exporter (same processor/tracer wiring as production). The manager
    runs Mod.start() as a scheduled task — the sleep lets it complete
    before the caller starts emitting (mirrors an engine awaiting its
    startup phase).
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setattr(
        OtelExportMod, "_make_exporter", lambda self, cfg: InMemorySpanExporter()
    )
    cfg = {"enabled": True, **(extra_cfg or {})}
    monkeypatch.setattr("nanobot.mods.manager._cache", {"otel_export": cfg})
    mgr = ModManager(bus, classes={"otel_export": OtelExportMod})
    started = mgr.start_all()
    assert started == ["otel_export"]
    mod = mgr._instances["otel_export"]
    await asyncio.sleep(0.02)  # let the scheduled start() task run
    assert mod._tracer is not None, "mod.start() did not complete"
    return mgr, mod


def _finished_spans(mod):
    provider = getattr(mod, "_provider", None)
    if provider is not None:
        provider.force_flush()
    # after stop(), the provider is gone but shutdown() already flushed
    return mod._exporter.get_finished_spans()


async def _drive_round(bus, engine) -> None:
    """One full round with one agent, one tool call, one LLM call."""
    await bus.emit("round:started", engine=engine, agents=["Kirk", "Harper"],
                   leader="Kirk", round_num=1)
    await bus.emit("agent:cycle_output", engine=engine, agent="Kirk",
                   chars=10, tools=["exec"])
    await bus.emit("tool:result", tool="exec", ok=True, chars=42)
    await bus.emit("llm:response", agent="Kirk", session="s-1", model="test-model",
                   input_tokens=100, output_tokens=40, cache_tokens=64,
                   cost=0.5, latency=1.5, error=None)
    await bus.emit("agent:done", engine=engine, agent="Kirk", reason="completed")
    await bus.emit("round:ended", engine=engine)


def _by_name(spans):
    return {s.name: s for s in spans}


# ── Off-by-default / zero-dependency guarantees ─────────────────────────────


class TestOffByDefault:
    async def test_disabled_mod_subscribes_nothing(self, bus, monkeypatch):
        monkeypatch.setattr("nanobot.mods.manager._cache", {})
        mgr = ModManager(bus, classes={"otel_export": OtelExportMod})
        assert mgr.start_all() == []
        assert bus.listener_count("llm:request") == 0
        assert bus.listener_count("llm:response") == 0
        # emitting with the mod merely discovered must be a no-op
        await bus.emit("round:started", engine=None, agents=[], leader=None, round_num=1)

    def test_module_import_never_pulls_opentelemetry(self):
        """Registry discovery imports builtin modules unconditionally —
        otel_export must import cleanly even with the SDK blocked."""
        code = (
            "import sys\n"
            "for _n in ('opentelemetry', 'opentelemetry.sdk',\n"
            "           'opentelemetry.exporter'):\n"
            "    sys.modules[_n] = None  # any import now raises\n"
            "import nanobot.mods.builtin.otel_export\n"
            "# sentinels above stay None; a real import would replace them\n"
            "bad = [k for k, v in sys.modules.items()\n"
            "       if k.startswith('opentelemetry') and v is not None]\n"
            "assert not bad, bad\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO_ROOT),
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr


# ── Span tree ───────────────────────────────────────────────────────────────


class TestSpanTree:
    async def test_workflow_agent_tool_model_tree(self, bus, monkeypatch):
        mgr, mod = await _start_mod(bus, monkeypatch)
        engine = object()
        await _drive_round(bus, engine)
        mgr.stop_all()
        await asyncio.sleep(0.02)

        spans = _finished_spans(mod)
        by_name = _by_name(spans)
        wf = by_name["workflow round-1"]
        agent = by_name["agent Kirk"]
        model = by_name["chat test-model"]
        tool = by_name["tool exec"]

        assert wf.parent is None or wf.parent.is_valid() is False
        # in this SDK span.parent is the parent's SpanContext directly
        assert agent.parent.span_id == wf.get_span_context().span_id
        assert model.parent.span_id == agent.get_span_context().span_id
        # tool:result has no agent in its payload; with exactly one open
        # agent span the mod attaches it there (documented heuristic)
        assert tool.parent.span_id == agent.get_span_context().span_id

    async def test_model_span_attributes_and_timing(self, bus, monkeypatch):
        mgr, mod = await _start_mod(bus, monkeypatch)
        engine = object()
        await _drive_round(bus, engine)
        mgr.stop_all()

        model = _by_name(_finished_spans(mod))["chat test-model"]
        attrs = dict(model.attributes)
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.request.model"] == "test-model"
        assert attrs["gen_ai.usage.input_tokens"] == 100
        assert attrs["gen_ai.usage.output_tokens"] == 40
        assert attrs["gen_ai.usage.cached_tokens"] == 64
        assert attrs["gen_ai.system"] == "nanobot"
        assert attrs["cost"] == 0.5
        # latency backdates the span start
        duration_ns = model.end_time - model.start_time
        assert duration_ns >= int(1.5 * 1e9)

    async def test_workflow_and_agent_attributes(self, bus, monkeypatch):
        mgr, mod = await _start_mod(bus, monkeypatch)
        engine = object()
        await _drive_round(bus, engine)
        mgr.stop_all()

        by_name = _by_name(_finished_spans(mod))
        wf_attrs = dict(by_name["workflow round-1"].attributes)
        agent_attrs = dict(by_name["agent Kirk"].attributes)
        tool_attrs = dict(by_name["tool exec"].attributes)
        assert wf_attrs["gen_ai.operation.name"] == "workflow"
        assert wf_attrs["gen_ai.system"] == "nanobot"
        assert agent_attrs["gen_ai.operation.name"] == "agent"
        assert agent_attrs["gen_ai.agent.name"] == "Kirk"
        assert tool_attrs["gen_ai.operation.name"] == "execute_tool"
        assert tool_attrs["gen_ai.tool.name"] == "exec"

    async def test_error_response_marks_span_error(self, bus, monkeypatch):
        mgr, mod = await _start_mod(bus, monkeypatch)
        engine = object()
        await bus.emit("round:started", engine=engine, agents=["Kirk"],
                       leader="Kirk", round_num=2)
        await bus.emit("agent:cycle_output", engine=engine, agent="Kirk",
                       chars=0, tools=[])
        await bus.emit("llm:response", agent="Kirk", session="s-1", model="m",
                       input_tokens=None, output_tokens=None, cache_tokens=0,
                       cost=None, latency=0.1, error="HTTP 503")
        await bus.emit("agent:done", engine=engine, agent="Kirk", reason="completed")
        await bus.emit("round:ended", engine=engine)
        mgr.stop_all()

        model = _by_name(_finished_spans(mod))["chat m"]
        assert model.status.status_code.name == "ERROR"

    async def test_round_ended_closes_lingering_agent_spans(self, bus, monkeypatch):
        """agent:done can be missed (cancel paths emit it, crash paths may
        not) — round:ended must still close everything it opened."""
        mgr, mod = await _start_mod(bus, monkeypatch)
        engine = object()
        await bus.emit("round:started", engine=engine, agents=["Kirk"],
                       leader="Kirk", round_num=1)
        await bus.emit("agent:cycle_output", engine=engine, agent="Kirk",
                       chars=5, tools=[])
        await bus.emit("round:ended", engine=engine)
        mgr.stop_all()
        await asyncio.sleep(0.02)

        spans = _finished_spans(mod)
        names = {s.name for s in spans}
        assert names == {"workflow round-1", "agent Kirk"}
        # a second round on the same engine starts fresh (no leaked state)
        assert mod._workflows == {}

    async def test_no_genai_names_leak_into_payloads(self, bus, monkeypatch):
        """The semconv vocabulary lives only in the mod's export layer."""
        raw: list[dict] = []

        async def _spy(**kw):
            raw.append(kw)

        bus.on("llm:response", _spy)
        bus.on("llm:request", _spy)
        mgr, mod = await _start_mod(bus, monkeypatch)
        engine = object()
        await _drive_round(bus, engine)
        mgr.stop_all()

        assert raw, "expected the spy to see llm events"
        for payload in raw:
            assert not any(str(k).startswith("gen_ai.") for k in payload)
            assert not any(
                isinstance(v, str) and v.startswith("gen_ai.")
                for v in payload.values()
            )


# ── Exporter wiring ─────────────────────────────────────────────────────────


class TestExporterWiring:
    def test_grpc_exporter_constructed_from_config(self):
        mod = OtelExportMod()
        exporter = mod._make_exporter(
            {"protocol": "grpc", "endpoint": "http://collector:4317"}
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcExporter,
        )
        assert isinstance(exporter, GrpcExporter)

    def test_http_exporter_constructed_from_config(self):
        mod = OtelExportMod()
        exporter = mod._make_exporter(
            {"protocol": "http", "endpoint": "http://collector:4318/v1/traces"}
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )
        assert isinstance(exporter, HttpExporter)

    def test_unknown_protocol_raises(self):
        mod = OtelExportMod()
        with pytest.raises(ValueError, match="protocol"):
            mod._make_exporter({"protocol": "carrier-pigeon"})
