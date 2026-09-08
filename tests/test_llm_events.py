"""llm:request / llm:response events emitted from the provider layer.

plan-2026-09-08 batch B step 1: the event catalogue previously stopped at
tool:result, so mods could not see token usage / cost. These tests pin the
new provider-emitted pair:

- ``llm:request`` fires before the call with agent/session/model; token /
  cost / latency fields are None (unknown pre-call).
- ``llm:response`` fires on every exit path (success, error, stream end)
  carrying the SAME values already parsed for LLMResponse / request_logs
  (no second parse — the fixtures assert numbers flow through the sources
  _parse_response / chunk collection already read).
- payload keys never contain gen_ai.* names — semconv mapping belongs to
  the otel_export mod export layer only.

Style follows tests/test_request_log_schema.py: real provider methods,
tiny fake response objects, only the network boundary patched.
Path.home() is redirected so request_logs never hits the real ~/.nanobot.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.groupchat.runtime.events import (
    EVENTS,
    BroadcastEventDispatcher,
    set_bus,
)
from nanobot.providers.httpx_provider import HttpxProvider
from nanobot.providers.litellm_provider import LiteLLMProvider


# ── Fixtures and tiny fakes ─────────────────────────────────────────────────


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def bus():
    b = BroadcastEventDispatcher()
    set_bus(b)
    yield b
    set_bus(None)


def _capture(bus: BroadcastEventDispatcher) -> list[tuple[str, dict]]:
    """Record (event_name, payload) for both llm events on *bus*."""
    seen: list[tuple[str, dict]] = []

    def _mk(name: str):
        async def _on(**kw):
            seen.append((name, kw))
        return _on

    bus.on("llm:request", _mk("llm:request"))
    bus.on("llm:response", _mk("llm:response"))
    return seen


def _usage(cached_tokens: int | None = None, prompt: int = 100, completion: int = 40):
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )
    if cached_tokens is not None:
        usage.prompt_tokens_details = SimpleNamespace(cached_tokens=cached_tokens)
    return usage


def _completion_response(usage, hidden: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=usage,
        _hidden_params=hidden if hidden is not None else {},
    )


def _chunk(*, content=None, finish_reason=None, usage=None, hidden=None) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage, _hidden_params=hidden or {})


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        raise StopAsyncIteration


_MESSAGES = [{"role": "user", "content": "hello"}]
_META = {"log_agent": "Kirk", "log_session": "s-1", "log_mode": "direct"}


# ── Catalogue registration ──────────────────────────────────────────────────


def test_llm_events_registered_in_catalogue():
    assert "llm:request" in EVENTS
    assert "llm:response" in EVENTS


# ── litellm provider ────────────────────────────────────────────────────────


class TestLiteLLMEmission:
    async def test_chat_emits_request_then_parsed_response(self, bus, log_home):
        seen = _capture(bus)
        resp = _completion_response(
            _usage(cached_tokens=64), {"response_cost": 0.0123}
        )
        with patch(
            "nanobot.providers.litellm_provider.acompletion",
            AsyncMock(return_value=resp),
        ):
            provider = LiteLLMProvider(default_model="test-model")
            out = await provider.chat(
                _MESSAGES, model="test-model", metadata=dict(_META)
            )

        assert [name for name, _ in seen] == ["llm:request", "llm:response"]
        name, req = seen[0]
        assert req["agent"] == "Kirk"
        assert req["session"] == "s-1"
        assert req["model"] == "test-model"
        assert req["input_tokens"] is None
        assert req["cost"] is None

        name, rsp = seen[1]
        assert name == "llm:response"
        # Same values LLMResponse carries (parsed once, reused):
        assert rsp["input_tokens"] == 100
        assert rsp["output_tokens"] == 40
        assert rsp["cache_tokens"] == 64
        assert rsp["cost"] == pytest.approx(0.0123)
        assert rsp["latency"] >= 0
        assert rsp["error"] is None
        assert out.cost == pytest.approx(0.0123)
        assert out.cache_tokens == 64

    async def test_chat_error_emits_response_with_error(self, bus, log_home):
        seen = _capture(bus)
        with patch(
            "nanobot.providers.litellm_provider.acompletion",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            provider = LiteLLMProvider(default_model="test-model")
            out = await provider.chat(
                _MESSAGES, model="test-model", metadata=dict(_META)
            )

        assert [name for name, _ in seen] == ["llm:request", "llm:response"]
        _, rsp = seen[1]
        assert "boom" in str(rsp["error"])
        assert rsp["input_tokens"] is None
        assert rsp["cost"] is None
        assert out.finish_reason == "error"

    async def test_chat_stream_emits_response_with_streamed_usage(self, bus, log_home):
        seen = _capture(bus)
        chunks = [
            _chunk(content="hel"),
            _chunk(
                content="lo",
                finish_reason="stop",
                usage=_usage(cached_tokens=128, prompt=200, completion=60),
                hidden={"response_cost": 0.02},
            ),
        ]
        with patch(
            "nanobot.providers.litellm_provider.acompletion",
            AsyncMock(return_value=_FakeStream(chunks)),
        ):
            provider = LiteLLMProvider(default_model="test-model")
            collected = [
                item
                async for item in provider.chat_stream(
                    _MESSAGES, model="test-model", metadata=dict(_META)
                )
            ]

        assert [name for name, _ in seen] == ["llm:request", "llm:response"]
        _, rsp = seen[1]
        assert rsp["agent"] == "Kirk"
        assert rsp["input_tokens"] == 200
        assert rsp["output_tokens"] == 60
        assert rsp["cache_tokens"] == 128
        assert rsp["cost"] == pytest.approx(0.02)
        assert rsp["latency"] >= 0
        assert collected[-1].cost == pytest.approx(0.02)

    async def test_no_metadata_still_emits_with_none_agent(self, bus, log_home):
        seen = _capture(bus)
        with patch(
            "nanobot.providers.litellm_provider.acompletion",
            AsyncMock(return_value=_completion_response(_usage())),
        ):
            provider = LiteLLMProvider(default_model="test-model")
            await provider.chat(_MESSAGES, model="test-model")

        _, rsp = seen[1]
        assert rsp["agent"] is None
        assert rsp["session"] is None


# ── httpx provider ──────────────────────────────────────────────────────────


class _FakeHttpxResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = str(self._data)

    def json(self):
        return self._data


class _FakeHttpxClient:
    def __init__(self, response):
        self._response = response

    async def post(self, *_a, **_kw):
        return self._response


class TestHttpxEmission:
    async def test_chat_emits_parsed_response(self, bus, log_home):
        seen = _capture(bus)
        data = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "total_tokens": 70,
                "cost": 0.5,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        }
        provider = HttpxProvider(default_model="test-model")
        with patch.object(
            provider, "_get_client", lambda: _FakeHttpxClient(_FakeHttpxResponse(200, data))
        ):
            out = await provider.chat(
                _MESSAGES, model="test-model", metadata=dict(_META)
            )

        assert [name for name, _ in seen] == ["llm:request", "llm:response"]
        _, req = seen[0]
        assert req["model"] == "test-model"
        assert req["agent"] == "Kirk"
        _, rsp = seen[1]
        assert rsp["input_tokens"] == 50
        assert rsp["output_tokens"] == 20
        assert rsp["cache_tokens"] == 30
        assert rsp["cost"] == pytest.approx(0.5)
        assert rsp["error"] is None
        assert out.cost == pytest.approx(0.5)

    async def test_chat_http_error_emits_response_with_error(self, bus, log_home):
        seen = _capture(bus)
        provider = HttpxProvider(default_model="test-model")
        with patch.object(
            provider,
            "_get_client",
            lambda: _FakeHttpxClient(_FakeHttpxResponse(503, {"error": "unavailable"})),
        ):
            out = await provider.chat(
                _MESSAGES, model="test-model", metadata=dict(_META)
            )

        assert [name for name, _ in seen] == ["llm:request", "llm:response"]
        _, rsp = seen[1]
        assert "503" in str(rsp["error"])
        assert rsp["cost"] is None
        assert out.finish_reason == "error"

    async def test_chat_stream_emits_response_with_usage(self, bus, log_home):
        seen = _capture(bus)
        chunks = [
            _chunk(content="hel"),
            _chunk(
                content="lo",
                finish_reason="stop",
                usage=_usage(cached_tokens=32, prompt=50, completion=20),
            ),
        ]

        class _FakeCompletions:
            async def create(self, **_kwargs):
                return _FakeStream(chunks)

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeOpenAIClient:
            chat = _FakeChat()

        with patch("openai.AsyncOpenAI", lambda **_kw: _FakeOpenAIClient()):
            provider = HttpxProvider(default_model="test-model")
            collected = [
                item
                async for item in provider.chat_stream(
                    _MESSAGES, model="test-model", metadata=dict(_META)
                )
            ]

        assert [name for name, _ in seen] == ["llm:request", "llm:response"]
        _, rsp = seen[1]
        assert rsp["agent"] == "Kirk"
        assert rsp["input_tokens"] == 50
        assert rsp["output_tokens"] == 20
        assert rsp["cache_tokens"] == 32
        assert rsp["cost"] is None  # documented: SDK streaming exposes no cost
        assert collected[-1].cache_tokens == 32


# ── Payload hygiene ─────────────────────────────────────────────────────────


async def test_payloads_carry_no_semconv_names(bus, log_home):
    """gen_ai.* mapping must exist only in the otel_export export layer."""
    seen = _capture(bus)
    with patch(
        "nanobot.providers.litellm_provider.acompletion",
        AsyncMock(return_value=_completion_response(_usage(cached_tokens=8))),
    ):
        provider = LiteLLMProvider(default_model="test-model")
        await provider.chat(_MESSAGES, model="test-model", metadata=dict(_META))

    for _, payload in seen:
        assert not any(str(k).startswith("gen_ai.") for k in payload)
        assert not any(
            isinstance(v, str) and v.startswith("gen_ai.") for v in payload.values()
        )
