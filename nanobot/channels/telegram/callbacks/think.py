"""Thinking-mode callback handlers."""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..formatting import to_cli_style


class ThinkCallbackMixin:
    """Mixin for think_* inline keyboard callbacks."""

    async def _handle_think_callback(self, query, data: str) -> None:
        """Handle /think interactive button callbacks."""
        engine = self._groupchat_engine
        if not engine:
            await query.edit_message_text("⚠️ 未配置群聊引擎")
            return

        if data.startswith("think_agent:"):
            # Show level-selection buttons for the chosen agent (or __all__)
            target = data[len("think_agent:"):]
            label = "全部 Agent" if target == "__all__" else target

            effort_options = [
                ("❌ 关闭", "off"),
                ("🔄 自动", "auto"),
                ("🔅 低", "low"),
                ("🔆 中", "medium"),
                ("✨ 高", "high"),
            ]
            buttons = [
                [InlineKeyboardButton(lbl, callback_data=f"think_set:{target}:{lvl}")]
                for lbl, lvl in effort_options
            ]
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="think_back")])
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
            await query.edit_message_text(
                f"🧠 设置思考强度 — {label}",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data.startswith("think_set:"):
            # Apply the setting: think_set:<name|__all__>:<level>
            parts = data.split(":", 2)
            if len(parts) != 3:
                return
            target, level_raw = parts[1], parts[2]

            effort: str | None = None if level_raw in ("off", "auto") else level_raw

            if target == "__all__":
                targets = list(engine.registry.keys())
            else:
                matched = engine._resolve_agent_name(target)
                if not matched:
                    await query.edit_message_text(f"❌ Agent '{target}' 不存在")
                    return
                targets = [matched]

            updated = []
            for name in targets:
                cfg = engine.registry.get(name)
                if cfg is not None:
                    cfg["reasoning_effort"] = effort
                    updated.append(name)
                    # Persist to disk
                    cfg_path = Path.home() / ".nanobot" / "agents" / name.lower() / "config.json"
                    if cfg_path.exists():
                        try:
                            disk_cfg = json.loads(cfg_path.read_text())
                            if effort:
                                disk_cfg["reasoning_effort"] = effort
                            else:
                                disk_cfg.pop("reasoning_effort", None)
                            cfg_path.write_text(json.dumps(disk_cfg, indent=2, ensure_ascii=False))
                        except Exception as e:
                            logger.warning("Failed to persist reasoning_effort for {}: {}", name, e)

            effort_display = effort or "off"
            names_str = ", ".join(updated)
            await query.answer(f"✅ {names_str} → {effort_display}", show_alert=False)

            # Refresh the status panel
            text, buttons = self._build_think_status_panel(engine)
            text = to_cli_style(text, title="🧠 Agent 思考模式")
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
            )

        elif data == "think_back":
            # Return to the main think status panel
            text, buttons = self._build_think_status_panel(engine)
            text = to_cli_style(text, title="🧠 Agent 思考模式")
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
            )

