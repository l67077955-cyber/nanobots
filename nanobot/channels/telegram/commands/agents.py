"""Agent management commands for Telegram."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.utils.helpers import split_message
from ..formatting import TELEGRAM_MAX_MESSAGE_LEN


class AgentCommandsMixin:
    """Mixin providing agent management commands."""

    def _ensure_gc_send(self, chat_id: str) -> None:
        """Ensure the group chat engine has send/edit callbacks for this chat.

        Callbacks are (re)bound whenever the requesting chat differs from the
        one currently bound — otherwise a second chat (e.g. a group after a
        private chat) would keep receiving the first chat's replies.
        """
        if self._groupchat_engine and (
            not self._groupchat_engine.has_send_fn
            or getattr(self, "_gc_bound_chat", None) != chat_id
        ):
            async def send_fn(text: str) -> None:
                await self._gc_send(chat_id, text)
            self._groupchat_engine.set_send_fn(send_fn)
            self._gc_bound_chat = chat_id
            # Set tool routing context so cron/message tools know the target
            self._groupchat_engine.set_tool_context("telegram", chat_id)

        if self._groupchat_engine and (
            not self._groupchat_engine.has_edit_fn
            or getattr(self, "_gc_bound_edit_chat", None) != chat_id
        ):
            int_chat_id = int(chat_id)

            async def send_and_get_id_fn(text: str) -> int | None:
                """Send a message and return its message_id for later editing."""
                if not self._app:
                    return None
                try:
                    msg = await self._app.bot.send_message(
                        chat_id=int_chat_id, text=text,
                    )
                    return msg.message_id
                except Exception as e:
                    logger.warning("gc_send_and_get_id failed: {}", e)
                    return None

            async def edit_fn(message_id: int, text: str) -> None:
                """Edit a previously sent message by ID."""
                if not self._app:
                    return
                try:
                    await self._app.bot.edit_message_text(
                        chat_id=int_chat_id,
                        message_id=message_id,
                        text=text,
                    )
                except Exception as e:
                    logger.debug("gc_edit failed (likely text unchanged): {}", e)

            self._groupchat_engine.set_edit_fn(edit_fn, send_and_get_id_fn)
            self._gc_bound_edit_chat = chat_id

        if self._groupchat_engine and not self._groupchat_engine.has_on_round_done:
            async def on_round_done() -> None:
                self._stop_typing(chat_id)
            self._groupchat_engine.set_on_round_done(on_round_done)

    async def _gc_send(self, chat_id: str, text: str) -> None:
        if not self._app:
            return
        for chunk in split_message(text, TELEGRAM_MAX_MESSAGE_LEN):
            await self._send_text(int(chat_id), chunk)

    async def _on_agents(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 未配置 agent")
            return
        registry = self._groupchat_engine.registry
        active = self._groupchat_engine.active_agents
        lines = ["📋 Agent 注册表:\n"]
        pm = self._load_pm()
        for name, info in registry.items():
            status = "🟢" if name in active else "⚪"
            leader = _d.agent_badge(name, self._groupchat_engine.leader)
            model = info.get("model", "?")
            # Tools summary
            from nanobot.groupchat.orchestra.engine import GroupChatEngine
            tools_cfg = info.get("tools")
            if isinstance(tools_cfg, dict):
                on = [k for k, v in tools_cfg.items() if v and k in GroupChatEngine.TOOL_NAMES]
                tools_str = ", ".join(on) if on else "无"
            elif info.get("tools_enabled", False):
                tools_str = "全部"
            else:
                tools_str = "无"
            # Find provider for this model
            prov_name = "默认"
            for pn, model_list in pm.get("models", {}).items():
                if model in model_list:
                    prov_name = pn
                    break
            lines.append(f"{status} {name}{leader}")
            lines.append(f"   🤖 {model} | 🏢 {prov_name} | 🔧 {tools_str}")
            lines.append("")
        if active:
            order = " → ".join(active)
            lines.append(f"👥 发言顺序: {order}")
        else:
            lines.append("💤 无活跃 agent")
        text = "\n".join(lines)
        # One ✏️ per agent → its edit panel (aligns with /editagent), plus Create.
        buttons = [
            [InlineKeyboardButton(f"✏️ {n}", callback_data=f"edit:{n}")] for n in registry
        ]
        buttons.append([InlineKeyboardButton("➕ 新建 Agent", callback_data="m:new_agent")])
        await update.message.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_setleader(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set or clear the leader agent."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return

        args = context.args or []
        if args:
            result = self._groupchat_engine.set_leader(args[0])
            await update.message.reply_text(result)
            return
        # Show selection panel; includes a clear button only when a leader exists.
        buttons = []
        for name in self._groupchat_engine.registry:
            mark = "👑 " if name == self._groupchat_engine.leader else ""
            buttons.append([InlineKeyboardButton(
                f"{mark}{name}", callback_data=f"sl:{name}"
            )])
        if self._groupchat_engine.leader:
            buttons.append([InlineKeyboardButton("🗑 清除 Leader", callback_data="sl:clear")])
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        await update.message.reply_text(
            "👑 选择 Leader:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def _on_addagent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 未配置 agent")
            return
        name = " ".join(context.args) if context.args else ""
        if name:
            self._ensure_gc_send(str(update.message.chat_id))
            result = self._groupchat_engine.add_agent(name)
            await update.message.reply_text(result)
            return
        # No args: show inline keyboard of available (inactive) agents
        active = set(self._groupchat_engine.active_agents)
        available = [(n, i) for n, i in self._groupchat_engine.registry.items() if n not in active]
        if not available:
            await update.message.reply_text("所有 agent 都已在对话中")
            return
        buttons = [[
            InlineKeyboardButton(f"➕ {n} ({i.get('model','?')})", callback_data=f"add:{n}"),
        ] for n, i in available]
        await update.message.reply_text("➕ 选择要加入的 Agent:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_removeagent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 未配置 agent")
            return
        name = " ".join(context.args) if context.args else ""
        if name:
            result = self._groupchat_engine.remove_agent(name)
            await update.message.reply_text(result)
            return
        # No args: show inline keyboard of active agents
        active = self._groupchat_engine.active_agents
        if not active:
            await update.message.reply_text("没有活跃 agent")
            return
        buttons = []
        for n in active:
            model = self._groupchat_engine.registry.get(n, {}).get("model", "?")
            buttons.append([InlineKeyboardButton(f"{n} ({model})", callback_data=f"rm:{n}")])
        await update.message.reply_text("➖ 选择要移除的 Agent:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_newagent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Interactive new agent creation: name → model → persona."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 未配置 agent")
            return
        name = " ".join(context.args) if context.args else ""
        if name and self._groupchat_engine.resolve_agent_name(name):
            await update.message.reply_text(f"⚠️ Agent '{name}' 已存在，用 /editagent 修改")
            return
        chat_id = str(update.message.chat_id)
        if name:
            self._begin_edit(chat_id, {"agent": name, "field": "create_model", "mode": "create"})
            await self._start_input(chat_id,
                f"🆕 创建 Agent: {name}\n\n"
                "请输入模型名:\n"
                "(如 anthropic/claude-sonnet-4-5, x-ai/grok-4.1-fast)"
            )
        else:
            self._begin_edit(chat_id, {"agent": "", "field": "create_name", "mode": "create"})
            await self._start_input(chat_id, "🆕 创建新 Agent\n\n请输入 Agent 名字:")

    async def _on_editagent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Interactive agent editing: name, persona, model."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 未配置 agent")
            return
        name = " ".join(context.args) if context.args else ""
        if not name:
            # Show inline keyboard of all agents
            agents = list(self._groupchat_engine.registry.keys())
            if not agents:
                await update.message.reply_text("没有可编辑的 agent")
                return
            buttons = []
            for n in agents:
                model = self._groupchat_engine.registry[n].get("model", "?")
                buttons.append([InlineKeyboardButton(f"{n} ({model})", callback_data=f"edit:{n}")])
            await update.message.reply_text("✏️ 选择要编辑的 Agent:", reply_markup=InlineKeyboardMarkup(buttons))
            return
        matched = self._groupchat_engine.resolve_agent_name(name)
        if not matched:
            await update.message.reply_text(f"❌ Agent '{name}' 不存在")
            return
        self._show_edit_menu(update, matched)

    def _edit_menu_text(self, agent_name: str) -> str:
        agent = self._groupchat_engine.registry[agent_name]
        tools_cfg = agent.get("tools")
        if isinstance(tools_cfg, dict):
            on = [k for k, v in tools_cfg.items() if v]
            tools_str = f"{len(on)}/{len(tools_cfg)} 开启" if on else "全部关闭"
        elif agent.get("tools_enabled", False):
            tools_str = "全部开启"
        else:
            tools_str = "全部关闭"
        rank = agent.get("rank", "pawn")
        rank_icons = {"pawn": "♟ 兵", "knight": "♞ 马", "bishop": "♝ 象", "queen": "♛ 后"}
        rank_str = rank_icons.get(rank, rank)
        effort = agent.get("reasoning_effort") or "off"
        effort_labels = {"off": "默认(自动)", "low": "低", "medium": "中", "high": "高"}
        effort_str = effort_labels.get(effort, effort)
        return (
            f"✏️ 编辑 {agent_name}\n\n"
            f"🎖️ 等级: {rank_str}\n"
            f"模型: {agent['model']}\n"
            f"🧠 思考强度: {effort_str}\n"
            f"工具: {tools_str}\n"
            f"人设: {agent['prompt'][:100]}..."
        )

    def _edit_menu_buttons(self, agent_name: str) -> InlineKeyboardMarkup:
        # ── 身份(改名/等级/人设)──
        identity = [
            [InlineKeyboardButton("✏️ 修改名字", callback_data=f"ef:{agent_name}:name")],
            [InlineKeyboardButton("🎖️ 更改等级", callback_data=f"ef:{agent_name}:rank")],
            [InlineKeyboardButton("📝 修改提示词", callback_data=f"ef:{agent_name}:persona")],
        ]
        # ── 能力(模型/工具/超参/思考)──
        capability = [
            [InlineKeyboardButton("🤖 更换模型/提供商", callback_data=f"ef:{agent_name}:model")],
            [InlineKeyboardButton("🔧 工具权限设置", callback_data=f"ef:{agent_name}:tools")],
            [InlineKeyboardButton("🧠 思考强度", callback_data=f"ef:{agent_name}:reasoning_effort")],
        ]
        # ── children = this agent's hyperparams (Create + per-child Delete on
        #    the parent panel, per the object-CRUD rule) ──
        agent = None
        if self._groupchat_engine and agent_name in self._groupchat_engine.registry:
            agent = self._groupchat_engine.registry[agent_name]
        agent_hp = (agent or {}).get("hyperparams") or {}
        if not isinstance(agent_hp, dict):
            agent_hp = {}
        hyperparams = []
        for k, v in agent_hp.items():
            hyperparams.append([
                InlineKeyboardButton(f"⚙️ {k} = {v}", callback_data=f"ahp:{agent_name}:{k}"),
                InlineKeyboardButton("🗑️", callback_data=f"ahp_del:{agent_name}:{k}"),
            ])
        if not hyperparams:
            hyperparams = [[InlineKeyboardButton("⚙️ 超参数设置", callback_data=f"ef:{agent_name}:hyperparams")]]
        hyperparams.append([InlineKeyboardButton("➕ 添加参数", callback_data=f"ahp_add:{agent_name}")])
        # ── 危险操作(删除,与取消分离)──
        danger = [
            [InlineKeyboardButton("🗑️ 删除 Agent", callback_data=f"da:{agent_name}")],
            [InlineKeyboardButton("❌ 取消", callback_data=f"ef:{agent_name}:cancel")],
        ]
        separator = [InlineKeyboardButton("━━━━━━━━", callback_data="noop")]
        return InlineKeyboardMarkup(
            identity + [separator] + capability + hyperparams + [separator] + danger
        )

    async def _show_edit_menu(self, update_or_query, agent_name: str) -> None:
        """Show edit menu for an agent (command entry → reply_text)."""
        if not (hasattr(update_or_query, 'message') and update_or_query.message):
            return
        text, markup = self._render_edit_menu(agent_name)
        await update_or_query.message.reply_text(text, reply_markup=markup)

    def _render_edit_menu(self, agent_name: str) -> tuple[str, InlineKeyboardMarkup]:
        """Pure render of an agent's edit panel (text + markup, no sends).

        Shared by /editagent entry and the '⬅️ 返回' (edit:{name}) navigation,
        so back re-renders the exact same panel.
        """
        return self._edit_menu_text(agent_name), self._edit_menu_buttons(agent_name)

