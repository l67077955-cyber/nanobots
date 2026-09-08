"""Schema tests for the persistent request log (~/.nanobot/request_logs/*.jsonl).

plan.md batch C0.1 (weakness W7): the parsing side (_parse_response /
chat_stream chunk collection) already extracts cost and cached-token usage
into LLMResponse, but the log writers only persisted prompt/completion/total.
These tests pin the success-entry contract after C0.1:

- usage keeps prompt/completion/total and gains cache_tokens (0 when the
  provider reports no cached tokens — mirrors LLMResponse.cache_tokens)
- cost is persisted at the entry top level (None when the provider reports
  none) — the shape telegram /log readers already expect via r.get("cost")
- error entries carry neither cost nor usage

Entries written before C0 lack the new fields entirely, so readers must stay
.get()-based; that old-entry read compatibility is pinned here by reading
the field-less error entry the same way readers do.

Style follows tests/test_user_ingress.py: real logging code, tiny fake
response objects, only external boundaries (litellm.acompletion / the openai
SDK constructor) patched.  Path.home() is redirected to a tmp dir so the
suite never writes into the real ~/.nanobot.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.providers.httpx_provider import HttpxProvider
from nanobot.providers.litellm_provider import LiteLLMProvider


# ── Fixtures and tiny fakes ─────────────────────────────────────────────────


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    """Redirect Path.home() to tmp_path; return the request_logs dir path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path / ".nanobot" / "request_logs"


def _read_entries(log_dir: Path) -> list[dict]:
    files = sorted(log_dir.glob("*.jsonl"))
    assert files, "expected a request_logs jsonl file to be written"
    lines = files[-1].read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _kwargs() -> dict:
    return {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"log_agent": "Kirk", "log_session": "s-1", "log_mode": "direct"},
    }


def _usage(cached_tokens: int | None = None, prompt: int = 100, completion: int = 40):
    """litellm/openai-SDK usage object; prompt_tokens_details optional."""
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )
    if cached_tokens is not None:
        usage.prompt_tokens_details = SimpleNamespace(cached_tokens=cached_tokens)
    return usage


def _completion_response(usage, hidden: dict | None = None) -> SimpleNamespace:
    """Minimal litellm ModelResponse shape consumed by _log_request."""
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
    """Minimal streaming chunk shape (litellm and openai SDK compatible)."""
    delta = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage, _hidden_params=hidden or {})


class _FakeStream:
    """Async-iterable stand-in for a streaming response."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        raise StopAsyncIteration


# ── litellm provider: non-streaming entries ─────────────────────────────────


class TestLiteLLMNonStreamEntry:
    def test_success_entry_persists_cache_tokens_and_cost(self, log_home):
        LiteLLMProvider._log_request(
            _kwargs(),
            response=_completion_response(_usage(cached_tokens=64), {"response_cost": 0.0123}),
            latency=0.5,
        )
        (rec,) = _read_entries(log_home)
        assert rec["status"] == "ok"
        assert rec["usage"] == {
            "prompt": 100,
            "completion": 40,
            "total": 140,
            "cache_tokens": 64,
        }
        assert rec["cost"] == 0.0123
        # Metadata attribution (W6 contract) is unchanged by C0.1.
        assert rec["agent"] == "Kirk"
        assert rec["mode"] == "direct"

    def test_success_entry_none_safe_when_provider_reports_neither(self, log_home):
        response = _completion_response(_usage())
        del response._hidden_params  # provider reported no cost at all
        LiteLLMProvider._log_request(_kwargs(), response=response, latency=0.1)
        (rec,) = _read_entries(log_home)
        assert rec["cost"] is None
        assert rec["usage"]["cache_tokens"] == 0
        assert rec["usage"]["prompt"] == 100

    def test_error_entry_has_no_cost_nor_usage(self, log_home):
        LiteLLMProvider._log_request(_kwargs(), error=RuntimeError("boom"), latency=0.0)
        (rec,) = _read_entries(log_home)
        assert rec["status"] == "error"
        assert "cost" not in rec
        assert "usage" not in rec
        # Old-entry read compatibility: readers must not assume the fields.
        assert rec.get("cost") is None
        assert rec.get("usage", {}).get("cache_tokens") is None


# ── litellm provider: streaming entries ─────────────────────────────────────


class TestLiteLLMStreamEntry:
    def test_stream_writer_persists_cache_tokens_and_cost(self, log_home):
        provider = LiteLLMProvider(default_model="test-model")
        provider._log_stream_request(
            _kwargs(),
            "ok",
            [],
            "stop",
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            0.2,
            cost=0.004,
            cache_tokens=8,
        )
        (rec,) = _read_entries(log_home)
        assert rec["status"] == "ok"
        assert rec["stream"] is True
        assert rec["usage"]["cache_tokens"] == 8
        assert rec["cost"] == 0.004

    async def test_chat_stream_wires_extracted_cost_and_cache_into_log(self, log_home):
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
                    _kwargs()["messages"], model="test-model"
                )
            ]

        # The final LLMResponse already carried both values (pre-C0.1
        # behavior); C0.1 additionally persists them into the log entry.
        assert collected[-1].cost == 0.02
        assert collected[-1].cache_tokens == 128
        (rec,) = _read_entries(log_home)
        assert rec["usage"] == {
            "prompt_tokens": 200,
            "completion_tokens": 60,
            "total_tokens": 260,
            "cache_tokens": 128,
        }
        assert rec["cost"] == 0.02


# ── httpx provider (OpenAI-compatible direct HTTP) ──────────────────────────


class TestHttpxEntry:
    @staticmethod
    def _write(log_home, *, response_data=None, error=None):
        HttpxProvider._log_request(
            model="test-model",
            api_base="https://api.test/v1",
            max_tokens=64,
            stream=False,
            params={"temperature": 0.9},
            tools_count=0,
            messages=[{"role": "user", "content": "hi"}],
            metadata={"log_agent": "Kirk"},
            response_data=response_data,
            error=error,
            latency=0.1,
        )
        (rec,) = _read_entries(log_home)
        return rec

    def test_success_entry_openai_compat_cost_and_cached_tokens(self, log_home):
        rec = self._write(
            log_home,
            response_data={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.002,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
            },
        )
        assert rec["usage"] == {
            "prompt": 10,
            "completion": 5,
            "total": 15,
            "cache_tokens": 4,
        }
        assert rec["cost"] == 0.002

    def test_success_entry_anthropic_native_cache_read_field(self, log_home):
        rec = self._write(
            log_home,
            response_data={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cache_read_input_tokens": 7,
                },
            },
        )
        assert rec["usage"]["cache_tokens"] == 7
        assert rec["cost"] is None

    def test_success_entry_none_safe_without_cost_or_cache(self, log_home):
        rec = self._write(
            log_home,
            response_data={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
        assert rec["cost"] is None
        assert rec["usage"]["cache_tokens"] == 0

    def test_error_entry_has_no_cost_nor_usage(self, log_home):
        rec = self._write(log_home, error=RuntimeError("connection refused"))
        assert rec["status"] == "error"
        assert "cost" not in rec
        assert "usage" not in rec

    async def test_chat_stream_wires_cache_tokens_into_log(self, log_home):
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
                    _kwargs()["messages"], model="test-model"
                )
            ]

        assert collected[-1].cache_tokens == 32
        (rec,) = _read_entries(log_home)
        assert rec["status"] == "ok"
        assert rec["stream"] is True
        assert rec["usage"] == {
            "prompt": 50,
            "completion": 20,
            "total": 70,
            "cache_tokens": 32,
        }
        # Cost stays None for httpx streaming: the openai SDK does not expose
        # response headers (documented limitation in chat_stream).
        assert rec["cost"] is None
