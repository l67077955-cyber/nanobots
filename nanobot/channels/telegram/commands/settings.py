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
from nanobot.groupchat.history.prompt_builder import (
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
        lines = [
            "⚙️ 默认超参数设置（全局）\n",
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
        text = "\n".join(lines)
        hp_text = to_cli_style(text, title="⚙️ 默认超参数设置（全局）")
        await self._app.bot.send_message(
            chat_id=int(chat_id), text=hp_text,
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
    }
    GC_SETTINGS_LABELS = {
        "tool_initial":           "初始工具额度 (每 agent × N)",
        "tool_earn_per_output":   "每次输出获得工具额度 (0.5=输出2次获地1点)",
        "allocate_timeout":       "消息分配超时 (秒)",
        "context_pool_capacity":  "对话池容量 (0=自动, >0=自定义)",
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
            _json.dumps({"chat_id": chat_id, "ts": ts})
        )

        await update.message.reply_text(f"🔄 正在重启...\n请求时间: {ts}")

        async def _do_restart():
            # Always do background restart: this ensures that if nanobot was started
            # from a command line / attached terminal (the "frontend"), that terminal
            # is released and the new instance runs fully detached in the background.
            # We still write the /tmp notice for telegram post-restart notification.
            from nanobot.utils.restart import perform_background_restart

            # Also set env notice so that any CLI restart notice consumer in the child can pick it up.
            # (The /tmp json is the primary mechanism for telegram channel notification.)
            try:
                from nanobot.utils.restart import set_restart_notice_to_env
                set_restart_notice_to_env(channel="telegram", chat_id=chat_id, metadata={})
            except Exception:
                pass

            perform_background_restart(delay_s=0.8)

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
        from nanobot.groupchat.orchestra.engine import GroupChatEngine
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
        """Display history management as a precise visual pipeline conversation."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        from nanobot.groupchat.history import history_settings as hs
        settings = hs.get_all()
        tr = settings["tool_results"]
        hist = settings["history"]
        cp = settings.get("context_pruning", {})

        engine = self._groupchat_engine
        current_msgs = len(engine._history) if engine else 0
        current_chars = sum(len(m.get("content", "")) for m in (engine._history if engine else []))
        compress_trigger = int(hist["max_messages"] * hist.get("compress_ratio", 0.8))
        ctx_chars_limit = settings["context_window_tokens"] * 4  # rough chars estimate

        ai_on = tr["summarize_enabled"]
        prune_soft_budget = int(ctx_chars_limit * cp.get("soft_ratio", 0.3))

        # ── Estimate compiled LLM context size per active agent ──
        # engine._history only stores final turn messages (user + agent final replies).
        # Tool calls live inside agent messages as appended text logs, not separate entries.
        # Actual LLM context = system prompts + history_to_messages(history).
        compiled_info = ""
        if engine and getattr(engine, "_active_agents", None):
            from nanobot.groupchat.history.prompt_builder import PromptBuilder
            parts = []
            for a in engine._active_agents:
                try:
                    compiled = PromptBuilder.history_to_messages(
                        engine._history, current_agent=a
                    )
                    c_chars = sum(len(m.get("content") or "") for m in compiled)
                    parts.append(f"{a}~{c_chars:,}字")
                except Exception:
                    parts.append(f"{a}:?")
            compiled_info = " | ".join(parts)

        status_line = (
            f"📊 历史轮次(不含工具调用): {current_msgs}/{hist['max_messages']}条"
            f" | {current_chars:,}/{hist['max_context_chars']:,}字\n"
            f"📌 编译后上下文(估): {compiled_info if compiled_info else '(engine未启动)'}"
        )

        text = (
            "─── 上下文管线 · 实时演示 ───\n"
            f"全局: context_window={settings['context_window_tokens']:,} tokens"
            f" | tool_result_max={settings['tool_result_max_chars']:,} 字符\n"
            f"历史: max_messages={hist['max_messages']}条(仅用户+agent最终回复)"
            f" | max_context_chars={hist['max_context_chars']:,}\n"
            f"ℹ️  工具调用结果以文本追加在agent消息内，不计入条数\n"
            f"{status_line}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "👤 用户: 帮我搜索特朗普的图片并下载\n"
            f" └─ 🛡 [头部永久保护] 此消息不压缩不截断\n"
            "\n"
            "── 轮次 1 ──\n"
            "🤖 Agent: 我来搜索… → web_search(query='特朗普图片')\n"
            "\n"
            f"📡 web_search 返回 12,000 字符:\n"
            f" └─ [截断①] web_search_max_chars={tr['web_search_max_chars']:,}\n"
            f"    原始 12,000 > {tr['web_search_max_chars']:,} → 仅保留前后各一半\n"
            f" └─ [截断②] tool_result_max_chars={settings['tool_result_max_chars']:,}\n"
            f"    全局硬上限，截断后结果不超此值\n"
            f" └─ [AI压缩] summarize_enabled={'✅' if ai_on else '❌'}"
            f" | 触发阈值=>{tr['summarize_threshold']:,}字符\n"
            f"    {'✅ 触发: ' if ai_on else '🚫 未触发(已关闭): '}"
            f"model={tr['summarize_model']}"
            f" | 最大输入={tr.get('summarize_max_input_chars', 8000):,}"
            f" | 最大输出={tr.get('summarize_max_output_chars', 4000):,}tokens\n"
            f"    → 摘要注入上下文(广播模式最大={tr.get('broadcast_result_max_chars', 20000):,}"
            f" | 直接模式最大={tr.get('direct_result_max_chars', 8000):,})\n"
            "\n"
            "── 轮次 2 ──\n"
            "🤖 Agent: 现在执行下载 → exec(python send_photo.py)\n"
            "\n"
            f"💻 exec 返回报错 3,000 字符:\n"
            f" └─ [截断①] exec_max_chars={tr['exec_max_chars']:,}\n"
            f"    3,000 < {tr['exec_max_chars']:,} → 未触发截断，完整保留\n"
            f" └─ [AI压缩] 3,000 < {tr['summarize_threshold']:,} → 未触发AI压缩\n"
            "\n"
            f"── [上下文裁剪] tool_loop 第2次迭代起自动检查 ──\n"
            f" 软裁剪: 上下文>{prune_soft_budget:,}字符({cp.get('soft_ratio',0.3)}×窗口)\n"
            f"   → 对超过soft_max_chars={cp.get('soft_max_chars',4000):,}的旧工具结果\n"
            f"      替换为精简摘要(仅保留路径/错误/kv)\n"
            f" 保护: 最近{cp.get('keep_recent',3)}轮的工具结果不裁剪\n"
            "\n"
            f"── [历史记忆压缩] 消息数>={compress_trigger}条触发 ──\n"
            f"   compress_ratio={hist.get('compress_ratio',0.8)} × max_messages={hist['max_messages']}"
            f" = {compress_trigger}条时触发\n"
            f"   🛡 头部保护: 首条消息+首条用户消息 → 永不压缩\n"
            f"   🗜 压缩中间段 → model={tr['summarize_model']}"
            f" | max_tokens={hist.get('compress_max_summary_tokens',600)}\n"
            f"   🛡 尾部保护: 最近6轮完整保留\n"
            "\n"
            "── 超过总限制时 ──\n"
            f"   max_messages={hist['max_messages']}条 或 max_context_chars={hist['max_context_chars']:,}字符\n"
            f"   → 从最早消息开始丢弃(tool_call与result配对一起丢)\n"
            f"\n{status_line}\n"
        )

        buttons = [
            [
                InlineKeyboardButton(
                    f"🌐 全局: ctx={settings['context_window_tokens']:,}tok / max_result={settings['tool_result_max_chars']:,}",
                    callback_data="hs_global",
                )
            ],
            [
                InlineKeyboardButton(
                    f"✂️ 工具截断: exec={tr['exec_max_chars']:,} fetch={tr['web_fetch_max_chars']:,} search={tr['web_search_max_chars']:,}",
                    callback_data="hs_stage1",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🧠 AI压缩: {'✅' if ai_on else '❌'} 阈值={tr['summarize_threshold']:,} 模型={tr['summarize_model'].split('/')[-1]}",
                    callback_data="hs_stage2",
                )
            ],
            [
                InlineKeyboardButton(
                    f"📚 历史: max={hist['max_messages']}条/{hist['max_context_chars']:,}字 压缩@{compress_trigger}条",
                    callback_data="hs_stage3",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🔪 迭代裁剪: soft@{cp.get('soft_ratio',0.3)} 保留最近{cp.get('keep_recent',3)}轮",
                    callback_data="hs_stage4",
                )
            ],
            [
                InlineKeyboardButton("🔄 重载配置", callback_data="hs_reload"),
            ],
        ]

        hist_text = to_cli_style(text, title="📚 上下文 & 历史")
        await update.message.reply_text(
            hist_text, reply_markup=InlineKeyboardMarkup(buttons),
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
        return "\n".join(lines), buttons

