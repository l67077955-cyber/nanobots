"""Lightweight dict-based i18n for nanobot.

Pure-Python catalog keyed by a stable string id, with ``{}``-style formatting.
Designed for incremental adoption: the Chinese string is the source-of-truth
and the fallback, so a missing key or an incomplete locale never breaks output
— it just shows Chinese.

Usage::

    from nanobot.i18n import i18n
    i18n.set_locale("en")                 # per-bot default (extend per-user later)
    greeting = i18n.t("ui.menu.title")    # -> "Management Panel"
    row = i18n.t("agent.row", n="Kirk", model="x")   # formatting

Catalog values: ``{ "en": "...", "zh": "..." }``. ``zh`` is optional; if absent
the first available locale's text is used as fallback. Any unknown ``key``
returns ``key`` itself (or a ``zh`` entry if one was registered).
"""
from __future__ import annotations

import threading

try:
    from functools import cache
except ImportError:  # pragma: no cover - py<3.9
    def cache(f):
        return f

_LOCAL = threading.local()
_DEFAULT_LOCALE = "zh"


class _I18n:
    """Thread-safe catalog + locale resolver."""

    def __init__(self) -> None:
        self._catalog: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    # ── locale ───────────────────────────────────────────────
    def set_locale(self, locale: str | None) -> None:
        _LOCAL.locale = locale

    def get_locale(self) -> str:
        return getattr(_LOCAL, "locale", None) or _DEFAULT_LOCALE

    # ── registration ─────────────────────────────────────────
    def register(self, key: str, *, en: str, zh: str) -> None:
        """Register or update one entry."""
        with self._lock:
            self._catalog[key] = {"en": en, "zh": zh}

    def register_many(self, entries: dict[str, dict[str, str]]) -> None:
        with self._lock:
            self._catalog.update(entries)

    # ── resolve ──────────────────────────────────────────────
    def t(self, key: str, locale: str | None = None, **format_kw) -> str:
        """Resolve ``key`` for ``locale`` (default: thread's locale, else en/zh).

        Falls back: requested locale -> zh -> first registered -> key itself.
        """
        loc = locale or self.get_locale()
        entry = self._catalog.get(key)
        if entry is None:
            out = key
        else:
            out = entry.get(loc) or entry.get("zh") or entry.get("en") or key
        if format_kw:
            try:
                out = out.format(**format_kw)
            except (KeyError, IndexError, ValueError):
                pass
        return out


# Module-level singleton.
i18n = _I18n()

# Convenience alias for the common call: T("key", **kw)
def T(key: str, **format_kw) -> str:
    return i18n.t(key, **format_kw)