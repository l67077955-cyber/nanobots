"""Main Telegram inline-keyboard callback dispatcher."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from loguru import logger

from .cb_agents import AgentCallbackMixin
from .cb_logs import LogsCallbackMixin
from .cb_prompts import PromptsCallbackMixin
from .cb_providers import ProvidersCallbackMixin


class CallbackCoreMixin(
    AgentCallbackMixin,
    LogsCallbackMixin,
    PromptsCallbackMixin,
    ProvidersCallbackMixin,
):
    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        logger.debug("Callback received: data={} from={}", query.data, query.from_user.id if query.from_user else "?")
        await query.answer()
        data = query.data
        chat_id = str(query.message.chat_id)
        try:
            if data == "noop":
                return
            for fn in (self._dispatch_agents, self._dispatch_logs, self._dispatch_prompts, self._dispatch_providers):
                if await fn(query, data, chat_id):
                    return
            if data.startswith("hs_"):
                await self._handle_history_callback(query, data)
            elif data.startswith("think_"):
                await self._handle_think_callback(query, data)
        except Exception as e:
            logger.exception("Callback error: data={}", data)
            try:
                await query.edit_message_text(f"❌ 按钮处理出错: {e}", parse_mode=None)
            except Exception:
                pass
