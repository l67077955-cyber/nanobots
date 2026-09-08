"""OTel GenAI export mod — workflow → agent → tool → model span tree.

plan-2026-09-08 batch B. Disabled by default; opt in via ``~/.nanobot/mods.json``::

    "otel_export": {
        "enabled": true,
        "endpoint": "http://localhost:4317",   // OTLP collector
        "protocol": "grpc",                    // grpc | http
        "service_name": "nanobot"
    }

Optional dependency: ``pip install 'nanobot[otel]'``. ALL opentelemetry
imports live inside :meth:`start` / handler bodies — registry discovery
imports builtin modules unconditionally, so merely having this file
installed must never require the SDK (pinned by
tests/test_otel_export.py). Disabled ⇒ zero subscriptions, zero imports,
zero overhead.

**Semconv containment (load-bearing).** The ``gen_ai.*`` attribute
vocabulary below is pre-stable (v1.41, no 1.0 — names WILL change). It
must exist ONLY in this file's ``_GEN_AI`` mapping: internal event
payloads, LLMResponse and request_logs never carry ``gen_ai.*`` names.
When the convention renames an attribute, edit ``_GEN_AI`` only.

**Span mapping.**
- ``round:started`` → ``round:ended``      : workflow span ``workflow round-N``
- first agent event → ``agent:done``       : agent span ``agent <name>``
- ``tool:result``                          : tool span ``tool <name>``
- ``llm:response`` (latency-backdated)     : model span ``chat <model>``

Parenting limits (event payloads are all the mod may see — AGENTS.md #3):
- ``tool:result`` carries no agent, so tool spans attach to the single
  open agent span when unambiguous, else to the latest workflow span.
  Exact parenting needs an ``agent`` field on ``tool:result`` — that is a
  core emitter change, deliberately out of this mod's scope.
- LLM events carry agent/session but no engine, so the agent span is
  resolved by name across open workflows (latest wins); llm responses
  with no resolvable parent become root spans (direct/one-shot calls).

Tier 1 (observe) — reads payloads, writes spans, nothing else.
"""

from __future__ import annotations

import time
from typing import Any

from nanobot.mods.base import Mod

# ── gen_ai.* attribute names — THE ONLY place they exist ────────────────────
_GEN_AI = {
    "system": "gen_ai.system",
    "operation": "gen_ai.operation.name",
    "request_model": "gen_ai.request.model",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cached_tokens": "gen_ai.usage.cached_tokens",
    "agent_name": "gen_ai.agent.name",
    "tool_name": "gen_ai.tool.name",
}


class OtelExportMod(Mod):
    name = "otel_export"
    version = "0.1"
    description = "OTLP GenAI span 导出(workflow→agent→tool→model),默认关闭"

    def default_config(self) -> dict[str, Any]:
        return {
            "endpoint": "http://localhost:4317",
            "protocol": "grpc",  # grpc | http
            "service_name": "nanobot",
        }

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self, ctx: Any) -> None:
        # All opentelemetry imports happen here (never at module import).
        from opentelemetry.context import Context
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        cfg = ctx.config
        self._exporter = self._make_exporter(cfg)
        provider = TracerProvider(
            resource=Resource.create(
                {"service.name": str(cfg.get("service_name", "nanobot"))}
            )
        )
        provider.add_span_processor(BatchSpanProcessor(self._exporter))
        self._Context = Context
        self._provider = provider
        self._tracer = provider.get_tracer("nanobot.mods.otel_export")
        # engine object → {"span": workflow span, "agents": {name: agent span}}
        self._workflows: dict[Any, dict[str, Any]] = {}
        # agent name → engine object owning the open agent span (latest wins)
        self._agent_owner: dict[str, Any] = {}

    async def stop(self) -> None:
        provider = getattr(self, "_provider", None)
        self._provider = None
        self._tracer = None
        self._workflows = {}
        self._agent_owner = {}
        if provider is not None:
            try:
                provider.shutdown()  # flush pending spans
            except Exception:  # noqa: BLE001 — shutdown must never raise
                pass

    def _make_exporter(self, cfg: dict[str, Any]) -> Any:
        """Build the OTLP exporter from mod config. The only network seam;
        tests swap this for the SDK's in-memory exporter."""
        proto = str(cfg.get("protocol", "grpc")).lower()
        endpoint = str(cfg.get("endpoint") or "http://localhost:4317")
        if proto == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            return OTLPSpanExporter(endpoint=endpoint, insecure=True)
        if proto == "http":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            return OTLPSpanExporter(endpoint=endpoint)
        raise ValueError(
            f"otel_export: unknown protocol {proto!r} (expected 'grpc' or 'http')"
        )

    # ── Parenting helpers ───────────────────────────────────────────────────

    def _root_ctx(self) -> Any:
        return self._Context()

    def _child_ctx(self, span: Any) -> Any:
        from opentelemetry.trace import set_span_in_context
        return set_span_in_context(span)

    def _agent_span(self, engine: Any, agent: str) -> Any | None:
        entry = self._workflows.get(engine)
        if entry is None:
            return None
        return entry["agents"].get(agent)

    def _ensure_agent_span(self, engine: Any, agent: str) -> Any | None:
        """Open the agent span on first sighting; idempotent afterwards."""
        if self._tracer is None:
            return None
        entry = self._workflows.get(engine)
        if entry is None:
            return None  # agent activity outside any round → no parent
        span = entry["agents"].get(agent)
        if span is not None:
            return span
        parent = entry["span"]
        span = self._tracer.start_span(
            f"agent {agent}",
            context=self._child_ctx(parent),
            attributes={
                _GEN_AI["system"]: "nanobot",
                _GEN_AI["operation"]: "agent",
                _GEN_AI["agent_name"]: agent,
            },
        )
        entry["agents"][agent] = span
        self._agent_owner[agent] = engine
        return span

    # ── Handlers ────────────────────────────────────────────────────────────

    async def on_round_started(self, *, engine: Any, agents: list, leader: Any,
                               round_num: Any, **kw: Any) -> None:
        if self._tracer is None or engine in self._workflows:
            return
        span = self._tracer.start_span(
            f"workflow round-{round_num}",
            context=self._root_ctx(),
            attributes={
                _GEN_AI["system"]: "nanobot",
                _GEN_AI["operation"]: "workflow",
                "round_num": round_num,
                "leader": leader,
            },
        )
        self._workflows[engine] = {"span": span, "agents": {}}

    async def on_round_ended(self, *, engine: Any, **kw: Any) -> None:
        entry = self._workflows.pop(engine, None)
        if entry is None:
            return
        for name, span in entry["agents"].items():
            if self._agent_owner.get(name) is engine:
                self._agent_owner.pop(name, None)
            try:
                span.end()
            except Exception:  # noqa: BLE001
                pass
        try:
            entry["span"].end()
        except Exception:  # noqa: BLE001
            pass

    async def on_agent_cycle_output(self, *, engine: Any, agent: str,
                                    chars: Any, tools: list, **kw: Any) -> None:
        self._ensure_agent_span(engine, agent)

    async def on_agent_done(self, *, engine: Any, agent: str, reason: Any,
                            **kw: Any) -> None:
        entry = self._workflows.get(engine)
        span = entry["agents"].pop(agent, None) if entry else None
        if self._agent_owner.get(agent) is engine:
            self._agent_owner.pop(agent, None)
        if span is not None:
            try:
                span.set_attribute("reason", str(reason))
                span.end()
            except Exception:  # noqa: BLE001
                pass

    async def on_tool_result(self, *, tool: str, ok: bool, chars: Any,
                             **kw: Any) -> None:
        if self._tracer is None:
            return
        # tool:result has no agent in its payload (core change, out of
        # scope): attach to the single open agent span when unambiguous,
        # else to the latest workflow span, else a root span.
        parent_ctx = self._root_ctx()
        open_agents = [
            (engine, entry)
            for engine, entry in self._workflows.items()
            for _ in entry["agents"]
        ]
        if len(open_agents) == 1:
            engine, entry = open_agents[0]
            parent_ctx = self._child_ctx(next(iter(entry["agents"].values())))
        elif self._workflows:
            latest = list(self._workflows)[-1]  # insertion order, newest last
            parent_ctx = self._child_ctx(self._workflows[latest]["span"])
        span = self._tracer.start_span(
            f"tool {tool}",
            context=parent_ctx,
            attributes={
                _GEN_AI["system"]: "nanobot",
                _GEN_AI["operation"]: "execute_tool",
                _GEN_AI["tool_name"]: tool,
                "ok": bool(ok),
                "chars": chars,
            },
        )
        span.end()

    async def on_llm_response(self, *, agent: Any, session: Any, model: Any,
                              input_tokens: Any, output_tokens: Any,
                              cache_tokens: Any, cost: Any, latency: Any,
                              error: Any = None, **kw: Any) -> None:
        if self._tracer is None:
            return
        from opentelemetry.trace import Status, StatusCode

        parent_ctx = self._root_ctx()
        if agent:
            owner = self._agent_owner.get(agent)
            span = self._agent_span(owner, agent) if owner is not None else None
            if span is not None:
                parent_ctx = self._child_ctx(span)
        elif self._workflows:
            parent_ctx = self._child_ctx(
                self._workflows[list(self._workflows)[-1]]["span"]
            )

        start_ns = None
        if latency is not None and latency >= 0:
            start_ns = time.time_ns() - int(float(latency) * 1e9)

        attrs = {
            _GEN_AI["system"]: "nanobot",
            _GEN_AI["operation"]: "chat",
        }
        if model:
            attrs[_GEN_AI["request_model"]] = model
        if input_tokens is not None:
            attrs[_GEN_AI["input_tokens"]] = input_tokens
        if output_tokens is not None:
            attrs[_GEN_AI["output_tokens"]] = output_tokens
        if cache_tokens:
            attrs[_GEN_AI["cached_tokens"]] = cache_tokens
        if cost is not None:
            attrs["cost"] = cost  # provider-reported units; not a semconv name
        if session:
            attrs["session"] = session

        span = self._tracer.start_span(
            f"chat {model or 'unknown'}",
            context=parent_ctx,
            start_time=start_ns,
            attributes=attrs,
        )
        if error:
            span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()
