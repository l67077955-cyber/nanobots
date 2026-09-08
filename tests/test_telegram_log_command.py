"""Rendering contract for the telegram /log page's cache indicator.

plan.md batch C1.4 follow-up to C0.1 (83b4b555a): request_logs entries
carry cached tokens under ``usage.cache_tokens`` — no writer has ever
produced a top-level ``cache_tokens`` (litellm_provider writes it inside
``record["usage"]`` for both stream and non-stream entries, ditto
httpx_provider), so the /log page builder reading
``r.get("cache_tokens")`` was a dead read and the 🔵 indicator could
never render. Pre-C0 success entries and all error entries carry no
``usage`` at all and must stay None-safe.

Style follows tests/test_user_ingress.py: the real LogCommandsMixin page
builder over plain log-entry dicts — no mocks.
"""

from __future__ import annotations

from nanobot.channels.telegram.commands.log import LogCommandsMixin


def _entry(**overrides) -> dict:
    """A realistic post-C0.1 success entry as _log_request writes it."""
    entry = {
        "ts": "2026-09-08T12:00:00",
        "session": "telegram:-100200300",
        "mode": "direct",
        "agent": "Kirk",
        "model": "deepseek/deepseek-chat",
        "status": "ok",
        "latency": 1.2,
        "reply_preview": "ok",
        "usage": {"prompt": 100, "completion": 40, "total": 140, "cache_tokens": 1234},
        "cost": 0.0123,
    }
    entry.update(overrides)
    return entry


def _render_entry_line(logs: list[dict]) -> str:
    """Render one page and return its last line (the log entry line)."""
    text, _markup = LogCommandsMixin()._build_log_page_v2(logs, page=0)
    return text.splitlines()[-1]


class TestLogPageCacheIndicator:
    def test_new_format_usage_cache_tokens_shows_indicator(self):
        """Post-C0.1 entries: cached tokens live under usage and render 🔵."""
        line = _render_entry_line([_entry()])
        assert line.startswith("✅")
        assert "🔵" in line
        # Adjacent C0.1 display contract: top-level cost renders in place.
        assert "$0.0123" in line

    def test_dead_top_level_cache_tokens_is_not_honored(self):
        """A top-level cache_tokens must NOT render 🔵 — pins the field
        location so the dead read path cannot silently come back."""
        line = _render_entry_line([
            _entry(cache_tokens=999, usage={"prompt": 100, "completion": 40, "total": 140})
        ])
        assert "🔵" not in line

    def test_pre_c0_entry_without_usage_renders_none_safe(self):
        """Entries written before C0 carry no usage at all — no crash."""
        line = _render_entry_line([_entry(usage=None)])
        assert "🔵" not in line
        assert "0tok" in line

    def test_error_entry_without_usage_renders_none_safe(self):
        """Error entries carry neither usage nor cost — no crash, no 🔵."""
        entry = _entry(status="error", error="RateLimitError")
        del entry["usage"]
        del entry["cost"]
        line = _render_entry_line([entry])
        assert line.startswith("❌")
        assert "🔵" not in line
