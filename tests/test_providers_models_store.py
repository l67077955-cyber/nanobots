from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.config.schema import ProviderConfig
from nanobot.webui import providers_models_store as store


@pytest.fixture
def pm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".nanobot"
    root.mkdir()
    monkeypatch.setattr(store, "providers_models_path", lambda: root / "providers_models.json")
    return root


def test_load_and_resolve_custom_provider(pm_home: Path) -> None:
    path = pm_home / "providers_models.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "闲鱼api": {"url": "http://example.com/v1", "apiKey": "sk-test"},
                },
                "models": {"闲鱼api": ["model-a"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    resolved = store.resolve_provider_from_store("闲鱼api")
    assert resolved is not None
    name, config = resolved
    assert name == "闲鱼api"
    assert config.api_key == "sk-test"
    assert config.api_base == "http://example.com/v1"


def test_upsert_provider_models_entry(pm_home: Path) -> None:
    store.upsert_provider_models_entry(
        "custom-hub",
        ProviderConfig(api_key="sk-new", api_base="https://hub.example/v1"),
    )

    data = json.loads((pm_home / "providers_models.json").read_text(encoding="utf-8"))
    assert data["providers"]["custom-hub"]["apiKey"] == "sk-new"
    assert data["providers"]["custom-hub"]["url"] == "https://hub.example/v1"
    assert data["models"]["custom-hub"] == []