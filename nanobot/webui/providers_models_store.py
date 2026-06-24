"""Bridge ~/.nanobot/providers_models.json into WebUI settings surfaces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.config.schema import ProviderConfig


def providers_models_path() -> Path:
    return Path.home() / ".nanobot" / "providers_models.json"


def load_providers_models() -> dict[str, Any]:
    path = providers_models_path()
    if not path.is_file():
        return {"providers": {}, "models": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"providers": {}, "models": {}}
    if not isinstance(data, dict):
        return {"providers": {}, "models": {}}
    providers = data.get("providers")
    models = data.get("models")
    return {
        "providers": providers if isinstance(providers, dict) else {},
        "models": models if isinstance(models, dict) else {},
    }


def save_providers_models(data: dict[str, Any]) -> None:
    path = providers_models_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def provider_config_from_entry(info: dict[str, Any]) -> ProviderConfig:
    return ProviderConfig(
        api_key=str(info.get("apiKey") or "").strip() or None,
        api_base=str(info.get("url") or "").strip() or None,
    )


def provider_entry_from_config(
    provider_config: ProviderConfig,
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = dict(previous or {})
    if provider_config.api_base:
        entry["url"] = provider_config.api_base
    elif "url" in entry and not provider_config.api_base:
        entry.pop("url", None)
    if provider_config.api_key:
        entry["apiKey"] = provider_config.api_key
    elif "apiKey" in entry and not provider_config.api_key:
        entry.pop("apiKey", None)
    return entry


def providers_models_items() -> list[tuple[str, ProviderConfig]]:
    rows: list[tuple[str, ProviderConfig]] = []
    for name, info in load_providers_models().get("providers", {}).items():
        if not isinstance(info, dict):
            continue
        rows.append((str(name), provider_config_from_entry(info)))
    return rows


def upsert_provider_models_entry(
    provider_name: str,
    provider_config: ProviderConfig,
) -> None:
    data = load_providers_models()
    providers = data.setdefault("providers", {})
    models = data.setdefault("models", {})
    previous = providers.get(provider_name) if isinstance(providers.get(provider_name), dict) else None
    providers[provider_name] = provider_entry_from_config(provider_config, previous=previous)
    models.setdefault(provider_name, models.get(provider_name, []))
    save_providers_models(data)


def resolve_provider_from_store(provider_name: str) -> tuple[str, ProviderConfig] | None:
    providers = load_providers_models().get("providers", {})
    info = providers.get(provider_name)
    if not isinstance(info, dict):
        normalized = provider_name.replace("-", "_")
        for key, value in providers.items():
            if not isinstance(value, dict):
                continue
            if key == provider_name or key.replace("-", "_") == normalized:
                info = value
                provider_name = str(key)
                break
        else:
            return None
    return provider_name, provider_config_from_entry(info)