"""Settings commands for Telegram (hyperparams, groupchat, restart, debug, prompt)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from loguru import logger

from nanobot.groupchat.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
)


class SettingsCommandsMixin:
    """Mixin providing settings commands."""

    async def _on_hyperparams(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """View or edit sampling parameters with interactive buttons."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        # Get provider's sampling params
        provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
        params = getattr(provider, 'sampling_params', None) if provider else None
        if not params:
            await update.message.reply_text("⚠️ 无法获取超参数（provider 不可用）")
            return

        await self._send_hyperparams_keyboard(str(update.message.chat_id), params)

    async def _send_hyperparams_keyboard(self, chat_id: str, params: dict) -> None:
        """Send hyperparams display with edit/delete buttons."""
        lines = ["⚙️ 当前超参数:\n"]
        buttons = []
        for k, v in params.items():
            lines.append(f"  {k}: {v}")
            buttons.append([
                InlineKeyboardButton(f"✏️ {k} = {v}", callback_data=f"hp:{k}"),
                InlineKeyboardButton("🗑", callback_data=f"hp_del:{k}"),
            ])
        buttons.append([InlineKeyboardButton("➕ 添加参数", callback_data="hp_add")])
        text = "\n".join(lines)
        await self._app.bot.send_message(
            chat_id=int(chat_id), text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Groupchat Settings ─────────────────────────────────────

    GC_SETTINGS_DEFAULTS = {
        "search_initial": 2,           # search pool = agents × N
        "search_earn_interval": 4,     # every N outputs earns +1 credit
        "allocate_timeout": 15,        # seconds before message is dropped
    }
    GC_SETTINGS_LABELS = {
        "search_initial":        "初始搜索额度 (每 agent × N)",
        "search_earn_interval":  "每 N 次对话返还 1 搜索额度",
        "allocate_timeout":      "消息分配超时 (秒)",
    }

    @staticmethod
    def _gc_settings_path() -> Path:
        return Path.home() / ".nanobot" / "groupchat_settings.json"

    def _load_gc_settings(self) -> dict:
        p = self._gc_settings_path()
        defaults = dict(self.GC_SETTINGS_DEFAULTS)
        if p.exists():
            try:
                saved = json.loads(p.read_text())
                defaults.update(saved)
            except Exception:
                pass
        return defaults

    def _save_gc_settings(self, data: dict) -> None:
        p = self._gc_settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))

    async def _on_groupchat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /groupchat command: view/edit groupchat parameters."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        settings = self._load_gc_settings()
        lines = ["⚙️ 群聊参数设置:\n"]
        buttons = []
        for key, label in self.GC_SETTINGS_LABELS.items():
            val = settings.get(key, self.GC_SETTINGS_DEFAULTS[key])
            lines.append(f"  {label}: {val}")
            buttons.append([InlineKeyboardButton(
                f"✏️ {label} = {val}",
                callback_data=f"gc:{key}",
            )])

        # Show pool capacity preview
        active = len(self._groupchat_engine.active_agents) if self._groupchat_engine else 0
        if active > 0:
            cap = active * (active - 1)
            search_pool = active * settings.get("search_initial", 1)
            lines.append(f"\n  对话池: {active} agents × {active - 1} = {cap} threads")
            lines.append(f"  搜索池: {active} agents × {settings.get('search_initial', 1)} = {search_pool} points")

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _on_endchat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            return
        if not self._groupchat_engine.active_agents:
            await update.message.reply_text("没有活跃 agent")
            return
        self._groupchat_engine.stop()
        await update.message.reply_text("⏹ 所有 agent 已移除")

    async def _on_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Hard restart: save state, spawn new process, terminate current."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        import json as _json
        import subprocess
        import sys
        import time as _time

        chat_id = str(update.message.chat_id)
        ts = _time.strftime("%H:%M:%S")

        # Save restart notification info
        Path("/tmp/nanobot_restart.json").write_text(
            _json.dumps({"chat_id": chat_id, "ts": ts})
        )

        await update.message.reply_text(f"🔄 正在重启...\n请求时间: {ts}")

        # Spawn new process
        subprocess.Popen(
            ["nanobot", "gateway"],
            cwd=str(Path.home() / ".nanobot"),
            start_new_session=True,
            stdout=open("/tmp/nanobot.log", "w"),
            stderr=subprocess.STDOUT,
        )

        # Give new process a moment to start, then exit
        await asyncio.sleep(1)
        import os
        os._exit(0)

    async def _on_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show comprehensive internal state for debugging."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        lines = ["🐛 Debug 状态\n"]
        engine = self._groupchat_engine
        if not engine:
            lines.append("⚠️ 群聊引擎未初始化")
            await update.message.reply_text("\n".join(lines))
            return

        # Toggle debug context logging
        engine._debug_context = not engine._debug_context
        lines.append(f"🔬 上下文日志: {'✅ 已开启' if engine._debug_context else '❌ 已关闭'}")
        lines.append("")

        # Engine state
        lines.append("⚙️ 引擎状态:")
        lines.append(f"  running: {engine._running}")
        lines.append(f"  task: {'✅ 活跃' if engine._task and not engine._task.done() else '❌ 无'}")
        lines.append(f"  send_fn: {'✅' if engine._send_fn else '❌ None'}")
        lines.append(f"  topic: {engine._topic[:50] or '(空)'}")
        lines.append("")

        # Current group
        group_name = getattr(engine, '_current_group_name', None)
        lines.append(f"📂 当前分组: {group_name or '(无)'}")

        # Leader
        lines.append(f"👑 领导者: {engine._leader or '(无)'}")

        # Speaking order
        if engine._active_agents:
            lines.append(f"📢 发言顺序: {' → '.join(engine._active_agents)}")
        else:
            lines.append("📢 发言顺序: (无活跃agent)")
        lines.append("")

        # Agents detail
        pm = self._load_pm()
        from nanobot.groupchat.engine import GroupChatEngine
        lines.append("👥 Agent 详情:")
        for name, info in engine.registry.items():
            active = "🟢" if name in engine._active_agents else "⚪"
            model = info.get("model", "?")
            # Resolve provider
            prov = "默认"
            for pn, ml in pm.get("models", {}).items():
                if model in ml:
                    prov = pn
                    break
            # Tools
            tools_cfg = info.get("tools", {})
            if isinstance(tools_cfg, dict) and any(k in GroupChatEngine.TOOL_NAMES for k in tools_cfg):
                on = [k for k, v in tools_cfg.items() if v and k in GroupChatEngine.TOOL_NAMES]
                tools_str = ",".join(on) if on else "无"
            elif info.get("tools_enabled"):
                tools_str = "全部"
            else:
                tools_str = "无"
            badge = " 👑" if engine._leader == name else ""
            lines.append(f"  {active} {name}{badge}")
            lines.append(f"    🤖 {model} | 🏢 {prov}")
            lines.append(f"    🔧 {tools_str}")
        lines.append("")

        # Stats
        lines.append("📊 统计:")
        lines.append(f"  历史消息: {len(engine._history)} 条")
        lines.append(f"  请求日志: {len(engine._request_log)} 条")
        lines.append(f"  输入队列: {engine._input_queue.qsize()} 条待处理")
        lines.append("")

        # Edit state
        chat_id = str(update.message.chat_id)
        es = self._edit_state.get(chat_id)
        if es:
            lines.append(f"📝 编辑状态: {es}")
        else:
            lines.append("📝 编辑状态: (无)")

        # Provider summary
        lines.append("\n🔗 Provider:")
        for pn, info in pm.get("providers", {}).items():
            url = (info.get("url") or "").rstrip("/")
            key = info.get("apiKey", "")
            key_preview = key[:8] + "..." if len(key) > 8 else key
            model_count = len(pm.get("models", {}).get(pn, []))
            lines.append(f"  {pn}: {url or '(native)'} ({model_count}模型) key={key_preview}")

        text = "\n".join(lines)
        await update.message.reply_text(text[:4096])

    # ── Prompt Orchestration ────────────────────────────────

    async def _on_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /prompt command: view/edit prompt components per agent."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        engine = self._groupchat_engine
        if not engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return

        # Directly show global component order editor
        text, markup = self._build_prompt_order_view(engine)
        await update.message.reply_text(text, reply_markup=markup)

    def _build_prompt_order_view(self, engine) -> tuple[str, "InlineKeyboardMarkup"]:
        """Build the global prompt component order view with edit/reorder buttons."""
        order = engine.prompt_builder.get_agent_prompt_order()
        overrides = PromptBuilder._load_prompt_overrides("__global__")
        labels = _COMPONENT_LABELS
        global_editable = _GLOBAL_EDITABLE
        agent_editable = _AGENT_EDITABLE

        lines = ["📝 提示词组件编排 (全局)\n"]
        for i, key in enumerate(order):
            if key in global_editable:
                icon = "✏️"
            elif key in agent_editable:
                icon = "📂"
            else:
                icon = "🔒"
            label = labels.get(key, key)
            tpl = overrides.get(key) or PromptBuilder.get_component_template(key)
            preview = f" — {len(tpl)}字" if tpl else ""
            lines.append(f"{i+1}. {icon} {label}{preview}")
        lines.append(f"\n✏️ = 全局模板  📂 = 每个agent独立 (/editagent)  🔒 = 自动生成")
        lines.append("💡 变量: {{agent}} {{members}} {{datetime}} {{round}} {{tools}} {{others}}")

        buttons = []
        for i, key in enumerate(order):
            row = []
            if key in global_editable:
                row.append(InlineKeyboardButton(f"✏️ {key}", callback_data=f"pre:__global__:{key}"))
            elif key in agent_editable:
                row.append(InlineKeyboardButton(f"📂 {key}", callback_data="pr:refresh"))
            else:
                row.append(InlineKeyboardButton(f"🔒 {key}", callback_data="pr:refresh"))
            if i > 0:
                row.append(InlineKeyboardButton("⬆️", callback_data=f"pru:{i}"))
            if i < len(order) - 1:
                row.append(InlineKeyboardButton("⬇️", callback_data=f"prd:{i}"))
            # Delete button (history cannot be removed)
            if key != "history":
                row.append(InlineKeyboardButton("❌", callback_data=f"prdel:{i}"))
            buttons.append(row)
        bottom_row = [InlineKeyboardButton("🔍 预览", callback_data="prv:0")]
        if engine.prompt_builder.get_available_components():
            bottom_row.insert(0, InlineKeyboardButton("➕ 添加组件", callback_data="pradd"))
        buttons.append(bottom_row)
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    async def _prompt_show_components(self, query) -> None:
        """Helper: refresh global prompt order view via callback."""
        engine = self._groupchat_engine
        text, markup = self._build_prompt_order_view(engine)
        await query.edit_message_text(text[:4096], reply_markup=markup)

    # ── Group Config Commands ───────────────────────────────

    async def _on_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display history management flow with interactive settings."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        from nanobot.groupchat import history_settings as hs
        settings = hs.get_all()
        tr = settings["tool_results"]
        hist = settings["history"]

        # Current stats
        engine = self._groupchat_engine
        current_msgs = len(engine._history) if engine else 0
        current_chars = sum(len(m.get("content", "")) for m in (engine._history if engine else []))

        summarize_status = "✅ 开启" if tr["summarize_enabled"] else "❌ 关闭"

        text = (
            "📊 历史管理流程\n\n"
            "━━ 全局设置 ━━\n"
            f"  上下文窗口 → {settings['context_window_tokens']:,} tokens\n"
            f"  工具结果截断 → {settings['tool_result_max_chars']:,} 字符\n\n"
            "━━ Stage 1: 工具输出截断 ━━\n"
            f"  exec       → 最大 {tr['exec_max_chars']:,} 字符\n"
            f"  web_fetch  → 最大 {tr['web_fetch_max_chars']:,} 字符\n"
            f"  web_search → 最大 {tr['web_search_max_chars']:,} 字符\n\n"
            "━━ Stage 2: AI 总结压缩 ━━\n"
            f"  触发阈值 → {tr['summarize_threshold']:,} 字符\n"
            f"  总结模型 → {tr['summarize_model']}\n"
            f"  状态     → {summarize_status}\n\n"
            "━━ Stage 3: 历史存储 ━━\n"
            f"  最大消息数 → {hist['max_messages']} 条\n"
            f"  最大上下文 → {hist['max_context_chars']:,} 字符\n"
            f"  当前消息数 → {current_msgs} 条\n"
            f"  当前上下文 → {current_chars:,} 字符\n"
        )

        buttons = [
            [
                InlineKeyboardButton("🌐 全局设置", callback_data="hs_global"),
                InlineKeyboardButton("📝 工具截断", callback_data="hs_stage1"),
            ],
            [
                InlineKeyboardButton("🤖 AI总结", callback_data="hs_stage2"),
                InlineKeyboardButton("📚 历史限制", callback_data="hs_stage3"),
            ],
            [
                InlineKeyboardButton("🔄 重载配置", callback_data="hs_reload"),
            ],
        ]

        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons),
        )

