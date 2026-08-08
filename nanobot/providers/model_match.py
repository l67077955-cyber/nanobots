"""Model-list hygiene + provider model routing helpers for nanobot.

Centralises two concerns shared by the Telegram provider/model UI (`callbacks.py`)
and the runtime router (`litellm_provider._resolve_pm_overrides`):

1. ``sanitize_model_list`` — strip human-edited separator/comment lines that
   accumulate in ``~/.nanobot/providers_models.json`` model arrays (e.g.
   ``"═══ Anthropic Claude ═══"``) so they can never be mistaken for real model
   IDs, pollute browse/search/speedtest, or break exact-match routing.

2. ``match_provider`` — a robust model→provider matcher. It uses a 4-stage
   cascade (exact → provider-prefix → normalized-family → *no fuzzy-to-nothing*)
   and **never silently falls back to a hardcoded default model**. When nothing
   matches it returns ``None`` so the caller can surface an explicit error
   ("not in any provider list") instead of "the message keeps using the wrong
   model no matter what I configure".
"""

from __future__ import annotations

import re
from typing import Any

# Characters that make a list item a separator/comment line, never a model ID.
_SEPARATOR_CHARS = set("═━=—–-_*~#·• \t")
# A model ID must be at least this long to be plausible (rules out "═", sections,
# stray tabs). Real IDs are >= 5 chars ("x-ai" family; shortest viable ~5).
_MIN_MODEL_LEN = 5


def sanitize_model_list(model_ids: list[str] | None) -> list[str]:
    """Return a clean list of real model IDs, dropping separator/comment junk.

    A line is dropped if it is empty, or contains only separator/whitespace
    characters, or starts with a separator run (``═══...``), or is too short to
    be a real model ID. The input list is not mutated.
    """
    out: list[str] = []
    for raw in model_ids or []:
        s = str(raw).strip()
        if not s:
            continue
        if s.startswith("═") or s.startswith("━") or s.startswith("="):
            continue
        if set(s) <= _SEPARATOR_CHARS:
            continue
        if len(s) < _MIN_MODEL_LEN:
            continue
        out.append(s)
    return out


def _family_name(model_id: str) -> str:
    """Normalise a model id to a stable 'family key' for fuzzy matching.

    Takes the last ``/``  segment (drops the vendor prefix), lowercases, strips
    ``:free``-style suffixes, and removes trailing date/version numeric groups
    (``-0731``, ``-02-23``, ``-1.2``, etc.). Examples::

        "deepseek/deepseek-v4-flash-0731" -> "deepseek-v4-flash"
        "deepseek-v4-flash"               -> "deepseek-v4-flash"
        "qwen/qwen3.5-flash-02-23"        -> "qwen3.5-flash"
    """
    seg = (model_id or "").split("/")[-1].lower().strip()
    seg = seg.split(":")[0]  # drop :free / :beta / :lite variants
    # Repeatedly strip trailing numeric groups (dates, version numbers).
    while re.search(r"[.\-]?\d+$", seg):
        seg = re.sub(r"[.\-]?\d+$", "", seg)
    return seg.strip(".")


_DEFAULT_NATIVE = frozenset({
    "openrouter", "anthropic", "openai", "deepseek", "groq",
    "gemini", "dashscope", "zhipu", "minimax", "moonshot",
    "siliconflow", "volcengine", "aihubmix",
})


def resolve_provider(
    pm: dict[str, Any],
    model: str | None,
    native_providers: frozenset[str] | set[str] | None = None,
) -> dict[str, str | None] | None:
    """Match ``model`` against a parsed providers_models payload.

    Returns None when nothing matches (caller must surface an explicit error),
    otherwise a dict with keys: ``provider_name``, ``api_base``, ``api_key``,
    ``model`` (the model string to send — None means "use the requested model"),
    and ``matched`` (the canonical model id that matched, for diagnostics).

    Cascade (first hit wins):
      1. exact match against a provider's (sanitized) model list
      2. provider-name prefix match (``<provider>/<rest>``)
      3. normalized-family match against any model in the list
    """
    if not model:
        return None
    provs = pm.get("providers", {}) or {}
    models = pm.get("models", {}) or {}

    # Sanitize every list once, lazily cached on first use within this call.
    clean: dict[str, list[str]] = {}
    for pn, lst in models.items():
        if isinstance(lst, list):
            clean[pn] = sanitize_model_list(lst)

    target = model.strip()
    native = frozenset(native_providers) if native_providers is not None else _DEFAULT_NATIVE

    def _make(pn: str, info: dict, matched: str) -> dict[str, str | None]:
        url = (info.get("url") or "").rstrip("/")
        # Custom/API-distributor providers need an openai/ prefix so LiteLLM
        # routes them through the OpenAI SDK; native providers keep the id.
        if pn in native:
            return {
                "provider_name": pn,
                "api_base": url if url else None,
                "api_key": info.get("apiKey") or None,
                "model": None,  # keep requested model as-is
                "matched": matched,
            }
        raw = target.split("/", 1)[1] if "/" in target else target
        return {
            "provider_name": pn,
            "api_base": url,
            "api_key": info.get("apiKey") or None,
            "model": f"openai/{raw}",
            "matched": matched,
        }

    # 1) Exact match.
    for pn, lst in clean.items():
        if target in lst:
            return _make(pn, provs.get(pn, {}), target)

    # 2) Provider-name prefix match: "<provider>/<rest>".
    prefix = target.split("/")[0] if "/" in target else ""
    if prefix and prefix in provs:
        return _make(prefix, provs[prefix], target)

    # 3) Normalized-family match. Prefer the provider whose sanitized list
    #    contains a candidate with an exactly-equal family name; otherwise any
    #    candidate whose family name starts with ours (or vice-versa).
    t_fam = _family_name(target)
    if t_fam:
        for pn, lst in clean.items():
            for cand in lst:
                if _family_name(cand) == t_fam:
                    return _make(pn, provs.get(pn, {}), cand)
        # Substring family fallback (e.g. "qwen-flash" vs "qwen3.5-flash").
        for pn, lst in clean.items():
            for cand in lst:
                c_fam = _family_name(cand)
                if c_fam and (c_fam.startswith(t_fam) or t_fam.startswith(c_fam)):
                    return _make(pn, provs.get(pn, {}), cand)

    return None


def describe_match(result: dict[str, str | None] | None, model: str) -> str:
    """Human-readable outcome for UI feedback (edit / add validations)."""
    if result is None:
        return f"模型 `{model}` 不在任何提供商列表中;请先用 /editprovider 拉取或手动添加"
    pn = result.get("provider_name") or "?"
    matched = result.get("matched") or model
    same = "✅" if matched == model else f"🔁 归一化匹配到 `{matched}` →"
    return f"{same} 路由到 **{pn}**"