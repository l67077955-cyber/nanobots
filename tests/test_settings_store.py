"""Unit tests for nanobot/state/settings_store.py — the single read/write point."""
import json
import pathlib

import pytest

from nanobot.state import settings_store as store


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the store's module-level paths at tmp so tests never touch ~/.nanobot."""
    monkeypatch.setattr(store, "PM_FILE", tmp_path / "providers_models.json")
    monkeypatch.setattr(store, "AGENTS_DIR", tmp_path / "agents")
    return tmp_path


# ── providers / models ─────────────────────────────────────────────

def test_pm_empty_defaults(isolated):
    assert store.load_pm() == {"providers": {}, "models": {}}


def test_add_list_update_delete_provider(isolated):
    store.add_provider("p1", "http://a/v1", "sk-1")
    assert store.list_providers() == {"p1": {"url": "http://a/v1", "apiKey": "sk-1"}}
    store.update_provider("p1", url="http://b/v1")
    assert store.list_providers()["p1"]["url"] == "http://b/v1"
    store.update_provider("p1", api_key="sk-2")
    assert store.list_providers()["p1"]["apiKey"] == "sk-2"
    # dup add rejected
    with pytest.raises(ValueError):
        store.add_provider("p1", "x", "y")
    # missing update rejected
    with pytest.raises(KeyError):
        store.update_provider("nope", url="http://x")
    store.delete_provider("p1")
    assert "p1" not in store.list_providers()


def test_add_delete_model(isolated):
    store.add_provider("p1", "http://a/v1", "k")
    store.add_model("p1", "vendor/model-one")
    store.add_model("p1", "vendor/model-two")
    assert store.list_models()["p1"] == ["vendor/model-one", "vendor/model-two"]
    with pytest.raises(ValueError):
        store.add_model("p1", "vendor/model-one")
    with pytest.raises(KeyError):
        store.add_model("ghost", "x")
    store.delete_model("p1", "vendor/model-one")
    assert store.list_models()["p1"] == ["vendor/model-two"]
    with pytest.raises(KeyError):
        store.delete_model("p1", "vendor/nope")
    # deleting provider drops its models
    store.delete_provider("p1")
    assert "p1" not in store.list_models()


def test_load_pm_self_heals_dirty_model_list(isolated):
    data = {"providers": {"p": {"url": "u", "apiKey": "k"}},
            "models": {"p": ["vendor/model-one", "═══ junk ═══", "vendor/model-two"]}}
    isolated.joinpath("providers_models.json").write_text(json.dumps(data))
    pm = store.load_pm()
    assert pm["models"]["p"] == ["vendor/model-one", "vendor/model-two"], "dirty separator line auto-removed"


# ── agents ─────────────────────────────────────────────────────────

def test_agent_crud_and_editable_whitelist(isolated):
    store.create_agent("Bob", "m1", "persona-bob")
    p = store.agent_config_path("Bob")
    assert p.exists()
    assert p.parent.joinpath("SOUL.md").read_text() == "persona-bob", "persona written to SOUL.md"
    assert store.load_agent("Bob")["model"] == "m1"

    store.update_agent("Bob", "model", "m2")
    assert store.load_agent("Bob")["model"] == "m2"
    store.update_agent("Bob", "tools_enabled", "false")
    assert store.load_agent("Bob")["tools_enabled"] is False, "bool coercion"

    with pytest.raises(ValueError):
        store.update_agent("Bob", "hax", "x")  # non-whitelisted field rejected

    with pytest.raises(ValueError):
        store.create_agent("Bob", "m3")  # already exists

    assert store.delete_agent("Bob") is True
    assert not p.exists()
    assert store.delete_agent("Bob") is False  # already gone


def test_agent_edit_missing_agent(isolated):
    with pytest.raises(KeyError):
        store.update_agent("Ghost", "model", "m")


# ── legacy (upstream HKUDS) agent-config sanitize ─────────────────

def test_sanitize_legacy_hyperparams_list(isolated):
    """Upstream writes hyperparams as [{param: [val, {env}]}, {env}]; load heals it."""
    cfg = {
        "model": "m1",
        "hyperparams": [
            {"temperature": [0.35, {"NANOBOT_MAXTOKENS": "16384"}],
             "top_p": [0.88, {"NANOBOT_MAXTOKENS": "16384"}]},
            {"NANOBOT_MAXTOKENS": "16384", "NANOBOT_CONTEXTWINDOWTOKENS": "200000"},
        ],
        "tools": [
            {"web_search": [True, {"NANOBOT_MAXTOKENS": "16384"}],
             "write_file": [False, {"NANOBOT_MAXTOKENS": "16384"}]},
            {"NANOBOT_MAXTOKENS": "16384"},
        ],
        "maxToolIterations": [50, {"NANOBOT_MAXTOKENS": "16384"}],
    }
    store.create_agent("Kirk", "m1")
    store.agent_config_path("Kirk").write_text(json.dumps(cfg))

    loaded = store.load_agent("Kirk")
    assert loaded["hyperparams"] == {"temperature": 0.35, "top_p": 0.88}, "env-override dict dropped, pairs unwrapped"
    assert loaded["tools"] == {"web_search": True, "write_file": False}
    assert loaded["maxToolIterations"] == 50

    # load_agent self-heals on disk (like load_pm)
    on_disk = json.loads(store.agent_config_path("Kirk").read_text())
    assert isinstance(on_disk["hyperparams"], dict) and on_disk["hyperparams"] == {"temperature": 0.35, "top_p": 0.88}


def test_sanitize_agent_idempotent_on_plain_dict(isolated):
    store.create_agent("Facet", "m1")
    store.agent_config_path("Facet").write_text(json.dumps({"model": "m1", "hyperparams": {"temperature": 0.1}}))
    assert store.load_agent("Facet")["hyperparams"] == {"temperature": 0.1}, "already-clean config untouched"

    cfg = {"hyperparams": {"temperature": 0.1}}
    assert store.sanitize_agent_config(cfg) is False