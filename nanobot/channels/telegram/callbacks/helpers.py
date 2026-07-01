"""Telegram callback helper utilities."""
from __future__ import annotations

import json
import re
from pathlib import Path

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from nanobot.config.validate import SAMPLING_KEYS


class CallbackHelpersMixin:
    """Shared keyboard builders for callback handlers."""

    @staticmethod
    def _sanitize_sampling_params(raw: dict | None) -> tuple[dict, list[str]]:
        """Keep only supported sampling keys so UI/runtime cannot drift."""
        if not isinstance(raw, dict):
            return {}, []
        clean = {k: v for k, v in raw.items() if k in SAMPLING_KEYS}
        ignored = sorted(set(raw) - set(clean))
        return clean, ignored

    def _sync_global_hyperparams_from_disk(self) -> dict:
        """Reload global hyperparams into the live provider and return them."""
        provider = getattr(self._groupchat_engine, "provider", None) if self._groupchat_engine else None
        params = getattr(provider, "sampling_params", None) if provider else None
        hp_path = Path.home() / ".nanobot" / "hyperparams.json"
        if hp_path.exists():
            try:
                saved = json.loads(hp_path.read_text())
                clean, ignored = self._sanitize_sampling_params(saved if isinstance(saved, dict) else {})
                if ignored:
                    logger.warning("hyperparams: ignored invalid keys from disk: {}", ignored)
                if params is not None:
                    # Merge defaults first, then overlay saved values,
                    # so partial files don't silently wipe other defaults.
                    from nanobot.providers.base import RECOMMENDED_AGENT_SAMPLING
                    params.clear()
                    params.update({**RECOMMENDED_AGENT_SAMPLING, **clean})
                return clean
            except Exception as e:
                logger.warning("Failed to sync hyperparams from disk: {}", e)
        return dict(params or {})

    def _sync_agent_settings_from_disk(self, agent_name: str) -> dict:
        """Refresh live registry fields that settings UI edits from config.json."""
        engine = self._groupchat_engine
        if not engine or agent_name not in engine.registry:
            return {}
        agent = engine.registry[agent_name]
        cfg_path = Path.home() / ".nanobot" / "agents" / agent_name.lower() / "config.json"
        if not cfg_path.exists():
            return agent
        try:
            cfg = json.loads(cfg_path.read_text())
        except Exception as e:
            logger.warning("Failed to sync agent config from {}: {}", cfg_path, e)
            return agent
        if not isinstance(cfg, dict):
            return agent

        for key in ("model", "rank", "tools", "tools_enabled", "reasoning_effort"):
            if key in cfg:
                agent[key] = cfg[key]
        if "hyperparams" in cfg:
            clean, ignored = self._sanitize_sampling_params(cfg.get("hyperparams"))
            effort = clean.pop("reasoning_effort", None)
            if effort is not None and "reasoning_effort" not in cfg:
                agent["reasoning_effort"] = effort
            if ignored:
                logger.warning("{} hyperparams: ignored invalid keys from disk: {}", agent_name, ignored)
            if clean:
                agent["hyperparams"] = clean
            else:
                agent.pop("hyperparams", None)
        return agent

    def _is_valid_sampling_key(self, key: str) -> bool:
        return key in SAMPLING_KEYS

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


    async def _send_agent_hyperparams_keyboard(self, chat_id: str, agent_name: str, agent_hp, query=None) -> None:
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
        markup = InlineKeyboardMarkup(buttons)
        if query is not None:
            await query.edit_message_text(text[:4096], reply_markup=markup)
        else:
            await self._app.bot.send_message(
                chat_id=int(chat_id), text=text[:4096],
                reply_markup=markup,
            )
