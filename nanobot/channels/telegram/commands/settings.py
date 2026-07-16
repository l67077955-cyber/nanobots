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

from nanobot.channels.telegram.formatting import to_cli_style
from nanobot.groupchat.context.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
    COMPONENT_PHASES as _COMPONENT_PHASES,
)


class SettingsCommandsMixin:
    """Mixin providing settings commands."""

    @staticmethod
    def _sync_hyperparams_from_disk(provider) -> bool:
        """Reload hyperparams.json into provider.sampling_params if file exists.
        Returns True if params were synced from disk."""
        if not provider:
            return False
        params = getattr(provider, 'sampling_params', None)
        if not params:
            return False
        hp_path = Path.home() / ".nanobot" / "hyperparams.json"
        if not hp_path.exists():
            return False
        try:
            saved = json.loads(hp_path.read_text())
            if isinstance(saved, dict):
                from nanobot.config.validate import SAMPLING_KEYS
                clean = {k: v for k, v in saved.items() if k in SAMPLING_KEYS}
                ignored = sorted(set(saved) - set(clean))
                if ignored:
                    logger.warning("hyperparams: ignored invalid keys from disk: {}", ignored)
                params.clear()
                params.update(clean)
                logger.info("Synced hyperparams from disk: {}", list(clean.keys()))
                return True
        except Exception as e:
            logger.warning("Failed to sync hyperparams from disk: {}", e)
        return False

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

        # Sync from disk so external file edits are reflected
        params = self._sync_global_hyperparams_from_disk()

        await self._send_hyperparams_keyboard(str(update.message.chat_id), params)

    async def _send_hyperparams_keyboard(self, chat_id: str, params: dict) -> None:
        """Send hyperparams display with edit/delete buttons."""
        lines = [
            "💡 新 agent 默认继承这些值。除非清楚每个参数的作用，否则建议保持精简或留空。\n",
            "简单控制思考/创意？推荐用 /editagent 里的「思考深度」+「等级」。\n"
        ]
        buttons = []
        for k, v in params.items():
            lines.append(f"  {k}: {v}")
            buttons.append([
                InlineKeyboardButton(f"✏️ {k} = {v}", callback_data=f"hp:{k}"),
                InlineKeyboardButton("🗑", callback_data=f"hp_del:{k}"),
            ])
        buttons.append([InlineKeyboardButton("➕ 添加参数", callback_data="hp_add")])
        buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
        text = "\n".join(lines)
        hp_text = to_cli_style(text, title="⚙️ 默认超参数设置（全局）")
        await self._app.bot.send_message(
            chat_id=int(chat_id) if chat_id.isdigit() else chat_id,
            text=hp_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )

    # ── Groupchat Settings ─────────────────────────────────────

    GC_SETTINGS_DEFAULTS = {
        "tool_initial": 2,               # tool pool = agents × N
        "tool_earn_per_output": 0.25,    # credits earned per output (float ok, e.g. 0.5 = 1 credit per 2 outputs)
        "allocate_timeout": 15,          # seconds before message is dropped
        "context_pool_capacity": 0,      # 0 = auto (n × (n-1)), >0 = custom capacity
        "context_points_per_agent": 0,   # 0 = disabled, >0 = custom points per agent
        "call_timeout": 90,              # per-agent LLM call timeout (seconds)
        "leader_call_timeout": 120,      # leader LLM call timeout (seconds)
        "global_timeout": 600,           # whole broadcast round hard limit (seconds)
    }
    GC_SETTINGS_LABELS = {
        "tool_initial":           "初始工具额度 (每 agent × N)",
        "tool_earn_per_output":   "每次输出获得工具额度 (0.5=输出2次获地1点)",
        "allocate_timeout":       "消息分配超时 (秒)",
        "context_pool_capacity":  "对话池容量 (0=自动, >0=自定义)",
        "context_points_per_agent": "对话池点数 (0=禁用, >0=每agent点数)",
        "call_timeout":           "Agent LLM 超时 (秒)",
        "leader_call_timeout":    "Leader LLM 超时 (秒)",
        "global_timeout":         "整轮广播超时 (秒)",
    }
    # Fields whose value is a float (default: int). tool_earn_per_output is a
    # ratio like 0.25; parsing it as int + rejecting <1 made it unsettable.
    GC_FLOAT_KEYS = {"tool_earn_per_output"}
    # Fields where 0 is a meaningful sentinel (auto/disabled). Other int
    # fields require >= 1.
    GC_ALLOW_ZERO_KEYS = {"context_pool_capacity", "context_points_per_agent"}

    @staticmethod
    def _gc_settings_path() -> Path:
        return Path.home() / ".nanobot" / "groupchat_settings.json"

    def _load_gc_settings(self) -> dict:
        p = self._gc_settings_path()
        defaults = dict(self.GC_SETTINGS_DEFAULTS)
        if p.exists():
            try:
                saved = json.loads(p.read_text())
                # Migrate old key names forward
                if "search_initial" in saved and "tool_initial" not in saved:
                    saved["tool_initial"] = saved.pop("search_initial")
                if "search_earn_interval" in saved and "tool_earn_per_output" not in saved:
                    old = saved.pop("search_earn_interval")
                    saved["tool_earn_per_output"] = round(1.0 / old, 4) if old else 0.25
                if "tool_earn_interval" in saved and "tool_earn_per_output" not in saved:
                    old = saved.pop("tool_earn_interval")
                    saved["tool_earn_per_output"] = round(1.0 / old, 4) if old else 0.25
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
        buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])

        # Show pool capacity preview
        active = len(self._groupchat_engine.active_agents) if self._groupchat_engine else 0
        if active > 0:
            pool_points = settings.get("context_pool_capacity", 0)
            auto_cap = active * (active - 1)
            cap = pool_points if pool_points > 0 else auto_cap
            pool_mode = f"手动({pool_points})" if pool_points > 0 else "自动"
            tool_pool = active * settings.get("tool_initial", 1)
            lines.append(f"\n  对话池: {pool_mode} → {cap} threads" + (f" (auto={auto_cap})" if pool_points > 0 else ""))
            lines.append(f"  工具池: {active} agents × {settings.get('tool_initial', 1)} = {tool_pool} points")

        gc_text = to_cli_style("\n".join(lines), title="⚙️ 群聊参数设置")
        await update.message.reply_text(
            gc_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )

    async def _on_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Hard restart: save notification info, then background-restart (close frontend, detach)."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        import json as _json
        import time as _time

        chat_id = str(update.message.chat_id)
        ts = _time.strftime("%H:%M:%S")

        # Save restart notification info (read by start() after reboot)
        Path("/tmp/nanobot_restart.json").write_text(
            _json.dumps({"chat_id": chat_id, "ts": ts, "started_at": _time.time()})
        )

        await update.message.reply_text(f"🔄 正在重启...\n请求时间: {ts}")

        async def _do_restart():
            await asyncio.sleep(0.8)  # let "正在重启" reply flush
            from nanobot.utils.restart import (
                is_systemd_service,
                perform_background_restart,
                perform_inplace_restart,
                set_restart_notice_to_env,
            )

            try:
                set_restart_notice_to_env(channel="telegram", chat_id=chat_id, metadata={})
            except Exception:
                pass

            if is_systemd_service():
                perform_inplace_restart()
            else:
                perform_background_restart(delay_s=0)

        asyncio.create_task(_do_restart())

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
            await update.message.reply_text(to_cli_style("\n".join(lines)), parse_mode="Markdown")
            return

        # Toggle debug context logging
        engine._debug_context = not engine._debug_context
        lines.append(f"🔬 上下文日志: {'✅ 已开启' if engine._debug_context else '❌ 已关闭'}")
        lines.append("")

        # Engine state
        lines.append("⚙️ 引擎状态:")
        lines.append(f"  running: {engine._running}")
        lines.append(f"  task: {'✅ 活跃' if engine._task and not engine._task.done() else '❌ 无'}")
        lines.append(f"  outbound: {'✅' if engine.has_outbound else '❌ None'}")
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
        from nanobot.groupchat.runtime.engine import GroupChatEngine
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
        lines.append(f"  历史消息: {len(engine.history)} 条")
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

        text = to_cli_style("\n".join(lines), title="🐛 Debug 状态")
        await update.message.reply_text(text[:4096], parse_mode="Markdown")

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
        text = to_cli_style(text, title="📋 PROMPT PIPELINE")
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

    # Components that are only injected under specific conditions
    _CONDITIONAL_TAGS: dict[str, str] = {
        "leader_prompt": "Leader",
        "broadcast_hint": "Group",
        "group_context": "Group",
        "group_nudge": "Group",
    }

    def _build_prompt_order_view(self, engine, manage_mode: bool = False) -> tuple[str, "InlineKeyboardMarkup"]:
        """Build the global prompt component order view.

        manage_mode=False: compact — one button per component (name only).
        manage_mode=True:  full — name + reorder/delete/visibility buttons.
        """
        order = engine.prompt_builder.get_agent_prompt_order()
        labels = _COMPONENT_LABELS
        global_editable = _GLOBAL_EDITABLE
        agent_editable = _AGENT_EDITABLE
        conditional_tags = self._CONDITIONAL_TAGS

        phases = _COMPONENT_PHASES

        lines = ["📋 PROMPT PIPELINE\n"]
        lines.append("▸ STATIC · before history · cache-friendly")
        lines.append("  → 💬 HISTORY · runtime")
        lines.append("▸ DYNAMIC · after history · per-turn\n")
        lines.append("🔒 auto · ✏️ global · 📂 agent · ● full · ○ empty · 👁 all · 👑 leader\n")

        display_num = 0
        prev_phase = None
        for i, key in enumerate(order):
            phase = phases.get(key, "static")
            if key == "history":
                lines.append("── 💬 HISTORY ──")
                continue

            # Phase section header on transition
            if phase != prev_phase:
                if phase == "static":
                    lines.append("▸ STATIC")
                else:
                    lines.append("▸ DYNAMIC")
                prev_phase = phase

            display_num += 1

            if key in global_editable:
                edit_icon = "✏️"
            elif key in agent_editable:
                edit_icon = "📂"
            else:
                edit_icon = "🔒"

            label = labels.get(key, key)
            tpl = PromptBuilder.get_component_template(key)
            status = f"●{len(tpl):,}字" if tpl else "○空"

            cond = conditional_tags.get(key, "")
            cond_str = f" · {cond}" if cond else ""

            vis = engine.prompt_builder.get_component_visibility(key)
            vis_icon = "👁" if vis == "all" else "👑"

            lines.append(f"  {display_num}. {edit_icon} {label} · {status}{cond_str} · {vis_icon}")

        lines.append("")

        buttons = []
        for i, key in enumerate(order):
            label = labels.get(key, key)

            # Name button (always shown)
            if key == "history":
                buttons.append([InlineKeyboardButton(f"📜 {label}", callback_data="pr:refresh")])
                continue

            if key in global_editable:
                tpl = PromptBuilder.get_component_template(key)
                dot = "●" if tpl else "○"
                buttons.append([InlineKeyboardButton(f"✏️{dot} {label}", callback_data=f"pre:__global__:{key}")])
            elif key in agent_editable:
                buttons.append([InlineKeyboardButton(f"📂 {label}", callback_data="pr:refresh")])
            else:
                buttons.append([InlineKeyboardButton(f"🔒 {label}", callback_data="pr:refresh")])

            # Action buttons — only in manage mode
            if manage_mode:
                row2 = []
                if i > 0:
                    row2.append(InlineKeyboardButton("⬆️", callback_data=f"pru:{i}"))
                if i < len(order) - 1:
                    row2.append(InlineKeyboardButton("⬇️", callback_data=f"prd:{i}"))
                row2.append(InlineKeyboardButton("🗑", callback_data=f"prdel:{i}"))
                vis = engine.prompt_builder.get_component_visibility(key)
                vis_btn = "👁" if vis == "all" else "👑"
                row2.append(InlineKeyboardButton(vis_btn, callback_data=f"pviz:{i}"))
                buttons.append(row2)

        # Bottom row
        bottom_row = []
        if manage_mode:
            bottom_row.append(InlineKeyboardButton("✅ 完成管理", callback_data="prmanage"))
        else:
            bottom_row.append(InlineKeyboardButton("⚙️ 管理", callback_data="prmanage"))
        bottom_row.append(InlineKeyboardButton("🔍 预览", callback_data="prv:0"))
        bottom_row.append(InlineKeyboardButton("📖 规则", callback_data="prrules"))
        if engine.prompt_builder.get_available_components():
            bottom_row.append(InlineKeyboardButton("➕ 添加", callback_data="pradd"))
        buttons.append(bottom_row)
        buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    async def _prompt_show_components(self, query, manage_mode: bool = False) -> None:
        """Helper: refresh global prompt order view via callback."""
        from telegram.error import BadRequest
        engine = self._groupchat_engine
        text, markup = self._build_prompt_order_view(engine, manage_mode=manage_mode)
        try:
            await query.edit_message_text(text[:4096], reply_markup=markup)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

    # ── Group Config Commands ───────────────────────────────

    async def _on_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display history management panel (shared builder with hs_* callbacks)."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        from nanobot.channels.telegram.settings_history_panel import build_history_panel

        text, markup = build_history_panel(self._groupchat_engine)
        hist_text = to_cli_style(text, title="📚 上下文 & 历史")
        await update.message.reply_text(
            hist_text, reply_markup=markup,
            parse_mode="Markdown",
        )

    # ── Think command ───────────────────────────────────────

    def _build_think_status_panel(self, engine) -> tuple[str, list]:
        """Build the /think status panel text and buttons."""
        lines = ["🧠 Agent 思考模式\n"]
        for name, cfg in engine.registry.items():
            effort = cfg.get("reasoning_effort") or "off"
            active_mark = "🟢" if name in engine.active_agents else "⚪"
            lines.append(f"  {active_mark} {name}: {effort}")

        buttons = []
        for name in engine.registry:
            buttons.append([InlineKeyboardButton(
                f"⚙️ {name}", callback_data=f"think_agent:{name}"
            )])
        buttons.append([InlineKeyboardButton(
            "🌐 全部 Agent", callback_data="think_agent:__all__"
        )])
        buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
        return "\n".join(lines), buttons

    async def _on_think(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display the agent thinking-depth panel."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        engine = self._groupchat_engine
        if not engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        text, buttons = self._build_think_status_panel(engine)
        text = to_cli_style(text, title="🧠 Agent 思考模式")
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
