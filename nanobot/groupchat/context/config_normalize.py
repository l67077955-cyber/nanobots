"""Normalize agent config.json values corrupted by env-substitution tooling."""

from __future__ import annotations

from typing import Any


def _is_env_metadata_dict(value: object) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return all(isinstance(k, str) and "NANOBOT_" in k for k in value)


def unwrap_config_value(value: Any) -> Any:
    """Recursively strip ``[payload, {NANOBOT_*: ...}]`` wrappers."""
    if isinstance(value, list):
        if len(value) == 2 and _is_env_metadata_dict(value[1]):
            return unwrap_config_value(value[0])
        return [unwrap_config_value(item) for item in value]
    if isinstance(value, dict):
        return {key: unwrap_config_value(item) for key, item in value.items()}
    return value


def normalize_agent_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a cleaned copy of an agent config dict."""
    normalized = unwrap_config_value(raw)
    if not isinstance(normalized, dict):
        return raw
    return normalized