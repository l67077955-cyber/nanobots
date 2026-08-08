"""Unit tests for nanobot.providers.model_match (model-list hygiene + routing)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nanobot.providers.model_match import (
    sanitize_model_list,
    _family_name,
    resolve_provider,
    describe_match,
)


# ---------- sanitize_model_list ----------
class TestSanitizeModelList:
    def test_removes_separator_lines(self):
        raw = [
            "═══ Anthropic Claude ═══",
            "anthropic/claude-opus-4.8",
            "═══ 免费精选 ═══",
            "openai/gpt-5-pro",
        ]
        clean = sanitize_model_list(raw)
        assert "anthropic/claude-opus-4.8" in clean
        assert "openai/gpt-5-pro" in clean
        assert all("══" not in m for m in clean)
        assert len(clean) == 2

    def test_removes_empty_and_junk(self):
        clean = sanitize_model_list(["", "   ", "━", "═══", "x", "deepseek/deepseek-v4-flash"])
        assert clean == ["deepseek/deepseek-v4-flash"]

    def test_does_not_mutate_input(self):
        raw = ["═══ x ═══", "a/model-1"]
        before = list(raw)
        sanitize_model_list(raw)
        assert raw == before


# ---------- _family_name ----------
class TestFamilyName:
    def test_strips_vendor_prefix_and_date_version(self):
        assert _family_name("deepseek/deepseek-v4-flash-0731") == "deepseek-v4-flash"
        assert _family_name("deepseek-v4-flash") == "deepseek-v4-flash"
        assert _family_name("qwen/qwen3.5-flash-02-23") == "qwen3.5-flash"

    def test_strips_free_suffix(self):
        assert _family_name("qwen/qwen3-coder:free") == "qwen3-coder"


# ---------- resolve_provider ----------
class TestResolveProvider:
    @pytest.fixture
    def pm(self):
        return {
            "providers": {
                "openrouter": {"url": "https://openrouter.ai/api/v1", "apiKey": "sk-or-x"},
                "国产模型api": {"url": "https://api.example.com/v1", "apiKey": "sk-cn"},
            },
            "models": {
                "openrouter": [
                    "═══ Anthropic Claude ═══",
                    "deepseek/deepseek-v4-flash",
                    "z-ai/glm-5",
                    "z-ai/glm-5.1",
                    "deepseek/deepseek-v4-flash-0731",
                ],
                "国产模型api": ["glm-5", "deepseek-v3.2"],
            },
        }

    def test_exact_match(self, pm):
        r = resolve_provider(pm, "deepseek/deepseek-v4-flash")
        assert r["provider_name"] == "openrouter"
        assert r["api_base"] == "https://openrouter.ai/api/v1"
        assert r["api_key"] == "sk-or-x"

    def test_exact_ignores_separator_pollution(self, pm):
        assert resolve_provider(pm, "═══ Anthropic Claude ═══") is None

    def test_provider_prefix_match(self, pm):
        r = resolve_provider(pm, "国产模型api/custom-thing")
        assert r["provider_name"] == "国产模型api"
        assert r["model"] == "openai/custom-thing"

    def test_fuzzy_family_match(self, pm):
        r = resolve_provider(pm, "deepseek-v4-flash")
        assert r["provider_name"] == "openrouter"
        assert r["matched"] in ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash-0731")

    def test_fuzzy_version_match(self, pm):
        r = resolve_provider(pm, "z-ai/glm-5.2")
        assert r["provider_name"] == "openrouter"
        assert r["matched"] in ("z-ai/glm-5", "z-ai/glm-5.1")

    def test_no_match_returns_none(self, pm):
        assert resolve_provider(pm, "totally/unknown-model-xyz") is None

    def test_none_model_returns_none(self, pm):
        assert resolve_provider(pm, None) is None


# ---------- describe_match ----------
class TestDescribeMatch:
    def test_match_renders_provider(self):
        pm = {
            "providers": {"openrouter": {"url": "https://x", "apiKey": "k"}},
            "models": {"openrouter": ["deepseek/deepseek-v4-flash"]},
        }
        r = resolve_provider(pm, "deepseek/deepseek-v4-flash")
        s = describe_match(r, "deepseek/deepseek-v4-flash")
        assert "openrouter" in s
        assert "✅" in s

    def test_none_match_renders_guidance(self):
        s = describe_match(None, "ghost/model")
        assert "不在任何提供商" in s
