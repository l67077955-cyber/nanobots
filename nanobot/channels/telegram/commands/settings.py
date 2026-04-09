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
            if isinstance(saved, dict) and saved:
                params.clear()
                params.update(saved)
                logger.info("Synced hyperparams from disk: {}", list(saved.keys()))
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
        self._sync_hyperparams_from_disk(provider)

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
        "context_pool_capacity": 0,    # 0 = auto (n × (n-1)), >0 = custom capacity
        "context_points_per_agent": 0, # 0 = disabled, >0 = custom points per agent
    }
    GC_SETTINGS_LABELS = {
        "search_initial":           "初始搜索额度 (每 agent × N)",
        "search_earn_interval":     "每 N 次对话返还 1 搜索额度",
        "allocate_timeout":         "消息分配超时 (秒)",
        "context_pool_capacity":    "对话池容量 (0=自动, >0=自定义)",
        "context_points_per_agent": "对话池点数 (0=禁用, >0=每agent点数)",
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
            pool_points = settings.get("context_pool_capacity", 0)
            auto_cap = active * (active - 1)
            cap = pool_points if pool_points > 0 else auto_cap
            pool_mode = f"手动({pool_points})" if pool_points > 0 else "自动"
            search_pool = active * settings.get("search_initial", 1)
            lines.append(f"\n  对话池: {pool_mode} → {cap} threads" + (f" (auto={auto_cap})" if pool_points > 0 else ""))
            lines.append(f"  搜索池: {active} agents × {settings.get('search_initial', 1)} = {search_pool} points")

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _on_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Hard restart: save notification info, then replace the current process."""
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
            _json.dumps({"chat_id": chat_id, "ts": ts})
        )

        await update.message.reply_text(f"🔄 正在重启...\n请求时间: {ts}")

        async def _do_restart():
            await asyncio.sleep(1)
            # Determine the correct command to restart
            # Use sys.executable to ensure we use the same Python interpreter
            # Reconstruct argv: if 'gateway' is in original args, keep it
            argv = sys.argv[:]
            if not any("gateway" in a for a in argv):
                # Running via entry point (e.g. /usr/local/bin/nanobot gateway)
                # sys.argv[0] is the script path
                argv = [sys.argv[0], "gateway"]

            logger.info("Restart: execv {} {}", sys.executable, [sys.executable] + argv)

            try:
                # os.execv replaces the current process in-place
                # All env vars, file descriptors are inherited automatically
                os.execv(sys.executable, [sys.executable] + argv)
            except Exception as e:
                # Fallback: spawn new process and exit
                logger.warning("execv failed ({}), falling back to Popen", e)
                import subprocess
                subprocess.Popen(
                    [sys.executable, "-m", "nanobot", "gateway"],
                    start_new_session=True,
                    stdout=open("/tmp/nanobot.log", "w"),
                    stderr=subprocess.STDOUT,
                )
                os._exit(0)

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

    # Components that are only injected under specific conditions
    _CONDITIONAL_TAGS: dict[str, str] = {
        "broadcast_hint": "广播模式",
        "leader_prompt": "Leader",
    }

    def _build_prompt_order_view(self, engine) -> tuple[str, "InlineKeyboardMarkup"]:
        """Build the global prompt component order view with edit/reorder buttons."""
        order = engine.prompt_builder.get_agent_prompt_order()
        labels = _COMPONENT_LABELS
        global_editable = _GLOBAL_EDITABLE
        agent_editable = _AGENT_EDITABLE
        conditional_tags = self._CONDITIONAL_TAGS

        history_idx = order.index("history") if "history" in order else len(order)
        pre_count = sum(1 for k in order[:history_idx] if k != "history")
        post_count = sum(1 for k in order[history_idx + 1:])

        lines = ["📋 System Prompt 组装管线 (全局)\n"]
        lines.append(f"↓ 系统上下文  ({pre_count} 个组件)\n")

        display_num = 0
        for i, key in enumerate(order):
            if key == "history":
                lines.append("┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
                lines.append("  💬 聊天记录（运行时自动插入）")
                lines.append("┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
                if post_count > 0:
                    lines.append(f"\n↓ 后置规范  ({post_count} 个组件)\n")
                continue

            display_num += 1
            if key in global_editable:
                edit_icon = "✏️"
            elif key in agent_editable:
                edit_icon = "📂"
            else:
                edit_icon = "🔒"

            label = labels.get(key, key)
            tpl = PromptBuilder.get_component_template(key)
            status = f"● {len(tpl):,}字" if tpl else "○ 空"

            cond = conditional_tags.get(key, "")
            cond_str = f"  [仅{cond}]" if cond else ""

            lines.append(f"{display_num}. {edit_icon} {label} — {status}{cond_str}")

        lines.append("")
        lines.append("✏️ 全局模板  📂 per-agent(/editagent)  🔒 自动生成")
        lines.append("● 已配置  ○ 空(跳过注入)  [条件] 按条件激活")
        lines.append("💡 变量: {{agent}} {{members}} {{datetime}} {{round}} {{tools}} {{others}}")

        buttons = []
        for i, key in enumerate(order):
            row = []
            label = labels.get(key, key)
            if key in global_editable:
                tpl = PromptBuilder.get_component_template(key)
                dot = "●" if tpl else "○"
                row.append(InlineKeyboardButton(f"✏️{dot} {label}", callback_data=f"pre:__global__:{key}"))
            elif key in agent_editable:
                row.append(InlineKeyboardButton(f"📂 {label}", callback_data="pr:refresh"))
            else:
                row.append(InlineKeyboardButton(f"🔒 {label}", callback_data="pr:refresh"))
            if i > 0:
                row.append(InlineKeyboardButton("⬆️", callback_data=f"pru:{i}"))
            if i < len(order) - 1:
                row.append(InlineKeyboardButton("⬇️", callback_data=f"prd:{i}"))
            # Delete button (history cannot be removed)
            if key != "history":
                row.append(InlineKeyboardButton("❌", callback_data=f"prdel:{i}"))
            buttons.append(row)
        bottom_row = [InlineKeyboardButton("🔍 预览完整上下文", callback_data="prv:0")]
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
        cp = settings.get("context_pruning", {})

        # Current stats
        engine = self._groupchat_engine
        current_msgs = len(engine._history) if engine else 0
        current_chars = sum(len(m.get("content", "")) for m in (engine._history if engine else []))

        summarize_status = "✅ 开启" if tr["summarize_enabled"] else "❌ 关闭"
        html_detect_status = "✅ 开启" if tr.get("html_detect_enabled", True) else "❌ 关闭"

        text = (
            "📊 历史管理流程\n\n"
            "━━ 全局设置 ━━\n"
            f"  上下文窗口 → {settings['context_window_tokens']:,} tokens\n"
            f"  工具结果截断 → {settings['tool_result_max_chars']:,} 字符\n\n"
            "━━ Stage 1: 工具输出截断 ━━\n"
            f"  exec       → 最大 {tr['exec_max_chars']:,} 字符\n"
            f"  web_fetch  → 最大 {tr['web_fetch_max_chars']:,} 字符\n"
            f"  web_search → 最大 {tr['web_search_max_chars']:,} 字符\n"
            f"  HTML 检测  → {html_detect_status}\n\n"
            "━━ Stage 2: AI 总结压缩 ━━\n"
            f"  触发阈值   → {tr['summarize_threshold']:,} 字符\n"
            f"  总结模型   → {tr['summarize_model']}\n"
            f"  最大输入   → {tr.get('summarize_max_input_chars', 8000):,} 字符\n"
            f"  最大输出   → {tr.get('summarize_max_output_chars', 4000):,} tokens\n"
            f"  广播模式   → {tr.get('broadcast_result_max_chars', 20000):,} 字符\n"
            f"  直接模式   → {tr.get('direct_result_max_chars', 8000):,} 字符\n"
            f"  状态       → {summarize_status}\n\n"
            "━━ Stage 3: 历史存储 ━━\n"
            f"  最大消息数 → {hist['max_messages']} 条\n"
            f"  最大上下文 → {hist['max_context_chars']:,} 字符\n"
            f"  压缩比例   → {hist.get('compress_ratio', 0.8)}\n"
            f"  压缩摘要   → {hist.get('compress_max_summary_tokens', 600)} tokens\n"
            f"  当前消息数 → {current_msgs} 条\n"
            f"  当前上下文 → {current_chars:,} 字符\n\n"
            "━━ Stage 4: 迭代上下文裁剪 ━━\n"
            f"  软裁剪比例 → {cp.get('soft_ratio', 0.3)}\n"
            f"  硬裁剪比例 → {cp.get('hard_ratio', 0.5)}\n"
            f"  保护最近   → {cp.get('keep_recent', 3)} 轮\n"
            f"  软裁剪阈值 → {cp.get('soft_max_chars', 4000):,} 字符\n"
            f"  保留头部   → {cp.get('soft_head_chars', 1500):,} 字符\n"
            f"  保留尾部   → {cp.get('soft_tail_chars', 1500):,} 字符\n"
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
                InlineKeyboardButton("✂️ 上下文裁剪", callback_data="hs_stage4"),
                InlineKeyboardButton("🔄 重载配置", callback_data="hs_reload"),
            ],
        ]

        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons),
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
        return "\n".join(lines), buttons

    async def _on_think(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /think command — shows interactive button panel."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        engine = self._groupchat_engine
        if not engine:
            await update.message.reply_text("⚠️ 未配置群聊引擎")
            return

        text, buttons = self._build_think_status_panel(engine)
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )

