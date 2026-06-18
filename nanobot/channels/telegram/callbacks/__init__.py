"""Telegram inline keyboard callbacks (split by domain)."""
from .helpers import CallbackHelpersMixin
from .history import HistoryCallbackMixin
from .think import ThinkCallbackMixin
from .edit import EditCallbackMixin
from .core import CallbackCoreMixin
from .param_docs import PARAM_DOCS


class CallbacksMixin(
    CallbackHelpersMixin,
    HistoryCallbackMixin,
    ThinkCallbackMixin,
    EditCallbackMixin,
    CallbackCoreMixin,
):
    """Combined mixin — imported by TelegramChannel."""

    _PARAM_DOCS = PARAM_DOCS


__all__ = ["CallbacksMixin"]
