"""Tests for config file validation."""

from nanobot.config.validate import SAMPLING_KEYS, validate_config_files


def test_sampling_keys_include_temperature():
    assert "temperature" in SAMPLING_KEYS


def test_validate_empty_returns_no_warnings(tmp_path, monkeypatch):
    import nanobot.config.validate as mod
    monkeypatch.setattr(mod, "_NANOBOT", tmp_path)
    assert validate_config_files() == []


def test_validate_detects_swapped_files(tmp_path, monkeypatch):
    import json
    import nanobot.config.validate as mod

    monkeypatch.setattr(mod, "_NANOBOT", tmp_path)
    (tmp_path / "hyperparams.json").write_text(
        json.dumps({"call_timeout": 180, "tool_initial": 2})
    )
    (tmp_path / "groupchat_settings.json").write_text(
        json.dumps({"temperature": 0.2, "top_p": 1.0})
    )
    warnings = validate_config_files()
    assert any("hyperparams" in w for w in warnings)
    assert any("groupchat_settings" in w for w in warnings)