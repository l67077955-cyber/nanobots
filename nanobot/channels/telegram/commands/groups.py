"""Group config commands for Telegram (save/load/del/groups/order)."""

from __future__ import annotations

import json
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from loguru import logger


class GroupCommandsMixin:
    """Mixin providing group config commands."""

    async def _on_savegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        args = (context.args or [])
        if not args:
            if not self._groupchat_engine.active_agents:
                await update.message.reply_text("⚠️ 没有活跃 agent，无法保存")
                return
            members = ', '.join(self._groupchat_engine.active_agents)
            chat_id = str(update.message.chat_id)
            self._edit_state[chat_id] = {"field": "sg_name"}
            await update.message.reply_text(f"👥 当前成员: {members}\n\n请输入分组名称（或发送 0 取消）:")
            return
        name = " ".join(args)
        result = self._groupchat_engine.save_group(name)
        await update.message.reply_text(result)

    async def _on_loadgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        chat_id = str(update.message.chat_id)
        self._ensure_gc_send(chat_id)
        args = (context.args or [])
        if not args:
            groups = self._groupchat_engine.load_groups()
            if not groups:
                await update.message.reply_text("📋 没有保存的分组\n用 /savegroup 保存当前成员")
                return
            buttons = []
            for gname, members in groups.items():
                buttons.append([InlineKeyboardButton(
                    f"{gname} ({', '.join(members)})",
                    callback_data=f"lg:{gname}"
                )])
            await update.message.reply_text("📂 选择要载入的分组:", reply_markup=InlineKeyboardMarkup(buttons))
            return
        name = " ".join(args)
        result = self._groupchat_engine.load_group(name)
        await update.message.reply_text(result)

    async def _on_delgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        args = (context.args or [])
        if not args:
            groups = self._groupchat_engine.load_groups()
            if not groups:
                await update.message.reply_text("📋 没有保存的分组")
                return
            buttons = []
            for gname, members in groups.items():
                buttons.append([InlineKeyboardButton(
                    f"🗑 {gname} ({', '.join(members)})",
                    callback_data=f"dg:{gname}"
                )])
            await update.message.reply_text("🗑 选择要删除的分组:", reply_markup=InlineKeyboardMarkup(buttons))
            return
        name = " ".join(args)
        result = self._groupchat_engine.delete_group(name)
        await update.message.reply_text(result)

    async def _on_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        result = self._groupchat_engine.list_groups()
        await update.message.reply_text(result)

    async def _on_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Change agent speaking order."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return

        agents = self._groupchat_engine.active_agents
        if len(agents) < 2:
            await update.message.reply_text("⚠️ 至少需要 2 个活跃 agent 才能调整顺序")
            return

        # Interactive: show current order with move buttons
        await self._send_order_keyboard(str(update.message.chat_id), agents)

    async def _send_order_keyboard(self, chat_id: str, agents: list[str]) -> None:
        """Send inline keyboard to reorder agents."""
        order_str = " → ".join(agents)
        text = f"📢 当前发言顺序:\n{order_str}\n\n点击 ⬆ 上移:"
        buttons = []
        for i, name in enumerate(agents):
            if i > 0:  # Can't move first one up
                buttons.append([InlineKeyboardButton(f"⬆ {name}", callback_data=f"ord:{i}")])
        buttons.append([InlineKeyboardButton("✅ 完成", callback_data="ord:done")])
        await self._app.bot.send_message(
            chat_id=int(chat_id), text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

