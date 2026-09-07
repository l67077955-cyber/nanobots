"""Callback handlers package for Telegram inline keyboards.

This package splits the monolithic callbacks.py (3299 lines) into focused modules:
- agents.py: Agent-related callbacks (add/rm/edit/delete, model selection)
- providers.py: Provider and model management callbacks
- settings.py: Bot configuration callbacks
- groups.py: Group management callbacks
- logs.py: Log viewing callbacks
- prompts.py: Prompt editing callbacks
- hyperparams.py: Hyperparameter tuning callbacks
"""

from nanobot.channels.telegram.callback_handlers.agents import AgentCallbacksMixin
from nanobot.channels.telegram.callback_handlers.providers import ProviderCallbacksMixin
from nanobot.channels.telegram.callback_handlers.settings import SettingsCallbacksMixin
from nanobot.channels.telegram.callback_handlers.groups import GroupCallbacksMixin
from nanobot.channels.telegram.callback_handlers.logs import LogCallbacksMixin
from nanobot.channels.telegram.callback_handlers.prompts import PromptCallbacksMixin
from nanobot.channels.telegram.callback_handlers.hyperparams import HyperparamsCallbacksMixin

__all__ = [
    "AgentCallbacksMixin",
    "ProviderCallbacksMixin",
    "SettingsCallbacksMixin",
    "GroupCallbacksMixin",
    "LogCallbacksMixin",
    "PromptCallbacksMixin",
    "HyperparamsCallbacksMixin",
]
