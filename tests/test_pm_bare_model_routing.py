"""Regression: pm override (providers_models.json) must win over gateway prefix.

Bug (2026-09-12 outage): when a bare model name (e.g. ``glm-5.1``) matched a
native provider in providers_models.json, ``_build_kwargs`` only adopted the
pm ``api_base``/``api_key`` but left ``pm_resolved`` False (because
``resolve_provider`` returns ``model=None`` for native hits, meaning "keep the
requested name"). The model string then fell through to ``_resolve_model()``,
which — in gateway mode (single provider initialised with e.g. an OpenRouter
key) — stacked the gateway prefix: ``glm-5.1`` → ``openrouter/glm-5.1``.
LiteLLM routes by that prefix to the gateway and IGNORES the pm api_base /
api_key, so a fully-configured native provider (zhipu/deepseek official
endpoints) was silently bypassed and every request hit the gateway instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.state import settings_store


PM_PAYLOAD = {
    "providers": {
        "openrouter": {
            "url": "https://openrouter.ai/api/v1",
            "apiKey": "sk-or-v1-testgatewaykey",
        },
        "zhipu": {
            "url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "apiKey": "zhipu-test-key",
        },
        "deepseek": {
            "url": "https://api.deepseek.com",
            "apiKey": "ds-test-key",
        },
    },
    "models": {
        "openrouter": ["z-ai/glm-5.1", "deepseek/deepseek-v4-pro"],
        "zhipu": ["glm-5.1", "glm-5.3"],
        "deepseek": ["deepseek-v4-pro", "deepseek-flash"],
    },
}


@pytest.fixture()
def pm_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "providers_models.json"
    f.write_text(json.dumps(PM_PAYLOAD), encoding="utf-8")
    monkeypatch.setattr(settings_store, "PM_FILE", f)
    return f


def _gateway_provider() -> LiteLLMProvider:
    """Provider initialised like the running gateway: OpenRouter key → gateway mode."""
    return LiteLLMProvider(api_key="sk-or-v1-testgatewaykey")


def test_pm_bare_native_model_bypasses_gateway_prefix(pm_file) -> None:
    """glm-5.1 + zhipu section in pm → must route to zhipu, not openrouter/."""
    p = _gateway_provider()
    assert p._gateway is not None, "fixture provider should be in gateway mode"
    kwargs = p._build_kwargs(
        [{"role": "user", "content": "hi"}], model="glm-5.1", max_tokens=16
    )
    assert not kwargs["model"].startswith("openrouter/"), (
        f"model {kwargs['model']!r} still routed to the gateway — "
        "pm api_base/api_key would be ignored by litellm"
    )
    assert kwargs.get("api_base") == "https://open.bigmodel.cn/api/coding/paas/v4"
    assert kwargs.get("api_key") == "zhipu-test-key"


def test_pm_bare_native_model_deepseek(pm_file) -> None:
    p = _gateway_provider()
    kwargs = p._build_kwargs(
        [{"role": "user", "content": "hi"}], model="deepseek-v4-pro", max_tokens=16
    )
    assert not kwargs["model"].startswith("openrouter/")
    assert kwargs.get("api_base") == "https://api.deepseek.com"
    assert kwargs.get("api_key") == "ds-test-key"


def test_pm_explicit_gateway_prefix_still_wins(pm_file) -> None:
    """A caller explicitly asking for z-ai/glm-5.1 (an openrouter-listed id)
    keeps gateway routing — pm prefix-match maps it to openrouter."""
    p = _gateway_provider()
    kwargs = p._build_kwargs(
        [{"role": "user", "content": "hi"}], model="z-ai/glm-5.1", max_tokens=16
    )
    # openrouter is the matched provider; its own url/key apply.
    assert kwargs.get("api_base") == "https://openrouter.ai/api/v1"
    assert kwargs.get("api_key") == "sk-or-v1-testgatewaykey"


def test_no_pm_match_keeps_gateway_behaviour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a pm file, the gateway prefix logic is untouched (legacy path)."""
    monkeypatch.setattr(settings_store, "PM_FILE", tmp_path / "nonexistent.json")
    p = _gateway_provider()
    kwargs = p._build_kwargs(
        [{"role": "user", "content": "hi"}], model="glm-5.1", max_tokens=16
    )
    assert kwargs["model"].startswith("openrouter/")
