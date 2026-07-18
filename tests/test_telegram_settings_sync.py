import json
from types import SimpleNamespace

import pytest

from nanobot.channels.telegram.callbacks.edit import EditCallbackMixin
from nanobot.channels.telegram.callbacks.helpers import CallbackHelpersMixin
from nanobot.channels.telegram.commands.settings import SettingsCommandsMixin
from nanobot.providers.base import RECOMMENDED_AGENT_SAMPLING


class _DummySettings(EditCallbackMixin, SettingsCommandsMixin, CallbackHelpersMixin):
    def __init__(self, tmp_path):
        self._edit_state = {}
        self.sent = []
        self._groupchat_engine = SimpleNamespace(
            provider=SimpleNamespace(sampling_params={"temperature": 0.9}),
            registry={"Kirk": {}},
        )
        self.tmp_path = tmp_path

    async def _gc_send(self, chat_id: str, content: str) -> None:
        self.sent.append((chat_id, content))


def test_sync_global_hyperparams_filters_invalid_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".nanobot").mkdir()
    (tmp_path / ".nanobot" / "hyperparams.json").write_text(
        json.dumps({"temperature": 0.2, "trump": 1.0}),
        encoding="utf-8",
    )
    dummy = _DummySettings(tmp_path)

    synced = dummy._sync_global_hyperparams_from_disk()

    assert synced == {"temperature": 0.2}
    # Sync merges saved values over RECOMMENDED_AGENT_SAMPLING defaults so
    # partial files don't silently wipe other defaults.
    expected = {**RECOMMENDED_AGENT_SAMPLING, "temperature": 0.2}
    assert dummy._groupchat_engine.provider.sampling_params == expected
    assert "trump" not in dummy._groupchat_engine.provider.sampling_params


@pytest.mark.asyncio
async def test_groupchat_settings_accept_zero_and_float(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".nanobot").mkdir()
    dummy = _DummySettings(tmp_path)

    dummy._edit_state["1"] = {"field": "gc_value", "gc_key": "context_pool_capacity"}
    await dummy._handle_edit_input("1", "0")

    dummy._edit_state["1"] = {"field": "gc_value", "gc_key": "tool_earn_per_output"}
    await dummy._handle_edit_input("1", "0.5")

    saved = json.loads((tmp_path / ".nanobot" / "groupchat_settings.json").read_text())
    assert saved["context_pool_capacity"] == 0
    assert saved["tool_earn_per_output"] == 0.5
