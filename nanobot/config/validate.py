"""Validate ~/.nanobot config files and emit clear warnings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

_NANOBOT = Path.home() / ".nanobot"

# Keys expected in each file — used to detect swapped/misplaced config.
SAMPLING_KEYS = frozenset({
    "temperature", "top_p", "top_k", "min_p", "repetition_penalty",
    "frequency_penalty", "presence_penalty", "top_a", "reasoning_effort",
})
_GC_KEYS = frozenset({
    "tool_initial", "tool_earn_per_output", "allocate_timeout",
    "context_pool_capacity", "context_points_per_agent",
    "call_timeout", "leader_call_timeout", "global_timeout",
    "conv_keep_turns", "memory_palace_path",
    # legacy aliases
    "search_initial", "search_earn_interval", "tool_earn_interval",
})


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("config: failed to read {}: {}", path.name, e)
        return {}


def _overlap(keys: set[str], known: frozenset[str]) -> set[str]:
    return {k for k in keys if k in known}


def validate_config_files() -> list[str]:
    """Check hyperparams.json / groupchat_settings.json for misplaced keys.

    Returns human-readable warning strings (empty if all OK).
    """
    warnings: list[str] = []
    hp = _read_json(_NANOBOT / "hyperparams.json")
    gc = _read_json(_NANOBOT / "groupchat_settings.json")

    hp_keys = set(hp)
    gc_keys = set(gc)

    hp_gc_overlap = _overlap(hp_keys, _GC_KEYS)
    gc_sampling_overlap = _overlap(gc_keys, SAMPLING_KEYS)
    hp_sampling = _overlap(hp_keys, SAMPLING_KEYS)
    gc_gc = _overlap(gc_keys, _GC_KEYS)

    if hp_gc_overlap and not hp_sampling:
        warnings.append(
            f"hyperparams.json 含群聊参数 {sorted(hp_gc_overlap)}，应移到 groupchat_settings.json"
        )
    elif hp_gc_overlap:
        warnings.append(
            f"hyperparams.json 混入群聊参数 {sorted(hp_gc_overlap)}（采样参数应单独存放）"
        )

    if gc_sampling_overlap and not gc_gc:
        warnings.append(
            f"groupchat_settings.json 含采样参数 {sorted(gc_sampling_overlap)}，应移到 hyperparams.json"
        )
    elif gc_sampling_overlap:
        warnings.append(
            f"groupchat_settings.json 混入采样参数 {sorted(gc_sampling_overlap)}"
        )

    # Detect temperature conflict between config.json and hyperparams.json.
    # config.json agents.defaults.temperature is the Pydantic default (used at
    # provider init), but hyperparams.json overrides it at runtime. If they
    # disagree, the user may be confused why editing config.json has no effect.
    main_cfg = _read_json(_NANOBOT / "config.json")
    cfg_temp = (main_cfg.get("agents", {}).get("defaults", {}) or {}).get("temperature")
    hp_temp = hp.get("temperature")
    if cfg_temp is not None and hp_temp is not None and abs(float(cfg_temp) - float(hp_temp)) > 0.01:
        warnings.append(
            f"temperature 冲突: config.json={cfg_temp} vs hyperparams.json={hp_temp}"
            " — hyperparams.json 优先 (在 /hyperparams 面板修改，不要改 config.json)"
        )

    return warnings


def log_effective_config(*, gc_settings: dict[str, Any] | None = None, sampling: dict[str, Any] | None = None) -> None:
    """One-line startup summary of runtime config (timeouts + sampling)."""
    if gc_settings is None:
        from nanobot.groupchat.runtime.broadcast_orchestrator import load_groupchat_settings
        gc_settings = load_groupchat_settings()
    if sampling is None:
        sampling = _read_json(_NANOBOT / "hyperparams.json")

    for w in validate_config_files():
        logger.warning("config: {}", w)

    logger.info(
        "config: gc call_timeout={}s leader={}s global={}s tool_initial={}",
        gc_settings.get("call_timeout", 90),
        gc_settings.get("leader_call_timeout", 120),
        gc_settings.get("global_timeout", 600),
        gc_settings.get("tool_initial", 2),
    )
    if sampling:
        sample_preview = {k: sampling[k] for k in ("temperature", "top_p", "top_k") if k in sampling}
        if sample_preview:
            logger.info("config: sampling {}", sample_preview)

    try:
        from nanobot.groupchat.history import history_settings as hs
        hs.reload()
        logger.info(
            "config: history max_msgs={} max_chars={} ctx_window={}",
            hs.max_messages(),
            hs.max_context_chars(),
            hs.get_context_window_tokens(),
        )
    except Exception:
        pass