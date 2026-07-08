"""History settings callback handlers.

Game-style grouped routing: memory / compress / tools / vis / global
"""
from __future__ import annotations

import json

from nanobot.channels.telegram.formatting import to_cli_style
from nanobot.channels.telegram.history_panel import (
    GROUPS,
    build_group_panel,
    build_history_panel,
    find_group_for_param,
    restore_defaults,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .param_docs import PARAM_DOCS


class HistoryCallbackMixin:
    """Mixin for hs_* inline keyboard callbacks."""

    _PARAM_DOCS = PARAM_DOCS

    async def _render_main_history_panel(self, query, *, expanded: bool = False) -> None:
        text, markup = build_history_panel(self._groupchat_engine, expanded=expanded)
        hist_text = to_cli_style(text, title="📚 历史与上下文设置")
        await query.edit_message_text(hist_text[:4096], reply_markup=markup, parse_mode="Markdown")

    async def _render_group_panel(self, query, group: str, *, advanced: bool = False) -> None:
        text, markup = build_group_panel(self._groupchat_engine, group, advanced=advanced)
        g = GROUPS.get(group, GROUPS["memory"])
        cli_text = to_cli_style(text, title=f"{g['icon']} {g['title']}")
        await query.edit_message_text(cli_text[:4096], reply_markup=markup, parse_mode="Markdown")

    async def _handle_history_callback(self, query, data: str) -> None:
        """Handle /history interactive settings callbacks."""
        from nanobot.groupchat.history import history_settings as hs

        # ── Reload ──
        if data == "hs_reload":
            hs.reload()
            await self._render_main_history_panel(query)
            return

        # ── Back / collapse demo ──
        if data in ("hs_back", "hs_demo:0"):
            await self._render_main_history_panel(query, expanded=False)
            return

        # ── Expand demo ──
        if data == "hs_demo:1":
            await self._render_main_history_panel(query, expanded=True)
            return

        # ── Group panel ──
        if data.startswith("hs_grp:"):
            group = data.split(":", 1)[1]
            await self._render_group_panel(query, group)
            return

        # ── Advanced toggle ──
        if data.startswith("hs_adv:"):
            parts = data.split(":")
            if len(parts) == 3:
                group = parts[1]
                advanced = parts[2] == "1"
                await self._render_group_panel(query, group, advanced=advanced)
            return

        # ── Restore defaults ──
        if data == "hs_rst":
            text = (
                "↩️ 恢复默认设置\n\n"
                "将所有历史与上下文参数恢复到默认值。\n"
                "此操作不可撤销。\n\n"
                "确认恢复全部？"
            )
            buttons = [
                [InlineKeyboardButton("✅ 确认恢复", callback_data="hs_rst:all:go")],
                [InlineKeyboardButton("⬅️ 取消", callback_data="hs_back")],
                [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data.startswith("hs_rst:"):
            parts = data.split(":", 2)
            if len(parts) == 3 and parts[2] == "go":
                group = parts[1]
                msg = restore_defaults(group)
                await query.answer(msg, show_alert=True)
                if group == "all":
                    await self._render_main_history_panel(query)
                else:
                    await self._render_group_panel(query, group)
            return

        # ── Toggle (hs_set:section:key:value) ──
        if data.startswith("hs_set:"):
            parts = data.split(":", 3)
            if len(parts) == 4:
                section, key, raw_val = parts[1], parts[2], parts[3]
                if raw_val in ("true", "false"):
                    value = raw_val == "true"
                elif raw_val.isdigit():
                    value = int(raw_val)
                else:
                    value = raw_val
                result = hs.update_field(section, key, value)
                await query.answer(result, show_alert=True)
                group = find_group_for_param(section, key)
                await self._render_group_panel(query, group)
            return

        # ── Edit (hs_edit:section:key) ──
        if data.startswith("hs_edit:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                section, key = parts[1], parts[2]
                if section == "__top__":
                    current = hs.get_all().get(key, "?")
                else:
                    current = hs.get_all().get(section, {}).get(key, "?")
                chat_id = str(query.message.chat_id)
                self._edit_state[chat_id] = {
                    "action": "history_setting",
                    "section": section,
                    "key": key,
                }
                doc_key = f"{section}:{key}"
                param_doc = self._PARAM_DOCS.get(doc_key)
                current_display = f"{current:,}" if isinstance(current, int) else str(current)
                if param_doc:
                    text = (
                        f"✏️ 修改: {param_doc['label']}\n\n"
                        f"📍 位置: {param_doc['location']}\n\n"
                        f"📖 说明:\n{param_doc['doc']}\n\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"当前值: {current_display}\n"
                        f"请输入新值:"
                    )
                else:
                    label = key if section == "__top__" else f"{section}.{key}"
                    text = (
                        f"✏️ 修改 {label}\n\n"
                        f"当前值: {current_display}\n\n"
                        f"请输入新值:"
                    )
                await query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
                    ]),
                )
            return
