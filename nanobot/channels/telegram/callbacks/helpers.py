"""Telegram callback helper utilities."""
from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


class CallbackHelpersMixin:
    """Shared keyboard builders for callback handlers."""

    @staticmethod
    def _sort_models_newest_first(model_ids: list[str]) -> list[str]:
        """Sort model IDs newest-to-oldest by extracting YYYYMMDD dates; reverse-alphabetical fallback."""
        def _key(mid: str) -> tuple:
            m = re.search(r'(20\d{6})', mid)
            return (int(m.group(1)) if m else 0, mid)
        return sorted(model_ids, key=_key, reverse=True)

    @staticmethod
    def _build_model_buttons_2col(
        model_ids: list[str],
        prov: str,
        existing: set[str],
        strip_prefix: str | None = None,
    ) -> list[list]:
        """Build 2-column inline keyboard buttons for a model list.

        Already-added models are listed in text only (no button).
        strip_prefix: if given, remove 'prefix/' from display label.
        """
        buttons: list[list] = []
        row: list = []
        for mid in model_ids:
            if mid in existing:
                continue  # shown in text, no button
            cb = f"ep_addm:{prov}:{mid}"
            if len(cb.encode()) > 64:
                continue
            label = mid[len(strip_prefix) + 1:] if strip_prefix and mid.startswith(f"{strip_prefix}/") else mid
            row.append(InlineKeyboardButton(f"+ {label}", callback_data=cb))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return buttons


    async def _send_agent_hyperparams_keyboard(self, chat_id: str, agent_name: str, agent_hp) -> None:
        if not isinstance(agent_hp, dict):
            agent_hp = {}
        """Send per-agent hyperparams keyboard."""
        buttons = []
        if agent_hp:
            for k, v in agent_hp.items():
                buttons.append([InlineKeyboardButton(f"✏️ {k} = {v}", callback_data=f"ahp:{agent_name}:{k}"),
                                InlineKeyboardButton("🗑️", callback_data=f"ahp_del:{agent_name}:{k}")])
        else:
            buttons.append([InlineKeyboardButton("（无参数）", callback_data="noop")])
        buttons.append([
            InlineKeyboardButton("➕ 添加参数", callback_data=f"ahp_add:{agent_name}"),
            InlineKeyboardButton("📥 复制全局设置", callback_data=f"ahp_sync:{agent_name}")
        ])
        buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{agent_name}")])
        text = (
            f"⚙️ {agent_name} 高级超参数（可选）\n\n"
            "这些是底层采样参数（temperature、top_p 等）。\n"
            "大多数用户只需用上方的「思考深度」即可获得想要的效果。\n"
            "除非你清楚知道每个参数的作用，否则建议保持为空（继承全局或模型默认）。"
        )
        if agent_hp:
            text += f"\n\n当前覆盖值：\n" + "\n".join(f"  {k} = {v}" for k, v in agent_hp.items())
        else:
            text += "\n\n（当前无覆盖，使用全局/默认）"
        await self._app.bot.send_message(
            chat_id=int(chat_id), text=text[:4096],
            reply_markup=InlineKeyboardMarkup(buttons),
        )

