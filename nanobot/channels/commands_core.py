"""Core slash commands shared by all channels."""

from __future__ import annotations

from typing import Any

from nanobot.bus.events import InboundMessage, OutboundMessage


class CoreCommandsMixin:
    """Start/help/cancel and engine control commands (/new /clear /stop)."""

    async def _on_start(self, update: Any, context: Any) -> None:
        if not update.message:
            return
        user = update.effective_user
        name = getattr(user, "first_name", None) or "there"
        hint = "" if getattr(self, "name", "") == "telegram" else " (web)"
        await update.message.reply_text(
            f"👋 Hi {name}! I'm nanobot{hint}.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )

    async def _on_help(self, update: Any, context: Any) -> None:
        if not update.message:
            return
        from nanobot.channels.telegram.formatting import to_cli_style

        help_text = to_cli_style(
            "🐈 nanobot commands:\n"
            "/new — 新对话\n"
            "/clear — 清空上下文\n"
            "/stop — 停止当前任务\n"
            "/cancel — 取消编辑 / 停止当前任务（万能中止）\n\n"
            "🎭 Agent 管理:\n"
            "/agents — 查看所有 agent\n"
            "/addagent <name> — 加入 agent\n"
            "/removeagent <name> — 移除 agent\n"
            "/newagent <name> — 创建新 agent（名字含 coder/code/engineer 等关键词会自动应用低温度配置，适合代码等任务；讨论型 agent 保持创意设置）\n"
            "/editagent <name> — 编辑 agent (名字/人设/模型/工具/超参数/预设)\n"
            "/hyperparams — 查看/修改超参数（全局用于普通 agent；per-agent 可独立覆盖）\n"
            "/restart — 硬重置（卡死时用）\n\n"
            "📁 分组管理：\n"
            "/savegroup <名称> — 保存当前成员\n"
            "/loadgroup <名称> — 载入分组\n"
            "/delgroup <名称> — 删除分组\n"
            "/groups — 查看所有分组\n"
            "/order — 调整发言顺序\n"
            "/setleader <name> — 设置/取消 Leader 👑\n\n"
            "🏢 提供商 & 模型：\n"
            "/providers — 查看提供商和模型\n"
            "/newprovider — 添加提供商\n"
            "/editprovider — 编辑提供商 (URL/Key/拉取模型)\n"
            "/deleteprovider — 删除提供商\n"
            "/newmodel — 添加模型\n"
            "/deletemodel — 删除模型\n"
            "/speedtest — 提供商测速\n\n"
            "📊 日志 & 调试：\n"
            "/log — 查看 LLM 调用记录 (tokens/延迟/工具)\n"
            "/prompt [agent] — 查看/编辑/排序提示词组件\n"
            "/summary — 生成对话总结\n\n"
            "⚙️ 群聊设置：\n"
            "/groupchat — 对话池/搜索预算等参数\n"
            "💡 加入 agent 后直接发消息即可对话\n"
            "2+ agent 自动进入群聊模式"
        )
        kwargs = {}
        if getattr(self, "name", "") == "telegram":
            kwargs["parse_mode"] = "Markdown"
        await update.message.reply_text(help_text, **kwargs)

    async def _forward_command(self, update: Any, context: Any) -> None:
        if not update.message or not update.effective_user:
            return
        sender = str(update.effective_user.id)
        if hasattr(self, "_sender_id"):
            sender = self._sender_id(update.effective_user)  # type: ignore[attr-defined]
        if not self.is_allowed(sender):
            return
        chat_id = str(update.message.chat_id)
        command = update.message.text or ""
        cmd = command.strip().split()[0].lower().split("@")[0]
        meta = {}
        msg = getattr(update.message, "message_id", None)
        if msg is not None:
            meta["message_id"] = msg
        thread_id = getattr(update.message, "message_thread_id", None)
        if thread_id is not None:
            meta["message_thread_id"] = thread_id

        if self._groupchat_engine:
            if cmd == "/stop":
                was_running = self._groupchat_engine._running
                self._groupchat_engine.stop()
                text = "✅ 群聊已停止。" if was_running else "ℹ️ 当前没有运行中的任务。"
                await self.bus.publish_outbound(OutboundMessage(
                    channel=self.name,
                    chat_id=chat_id,
                    content=text,
                    metadata=meta,
                ))
            elif cmd in ("/clear", "/new"):
                self._groupchat_engine.clear_history()
                action = "新对话已开始" if cmd == "/new" else "上下文已清空"
                await self.bus.publish_outbound(OutboundMessage(
                    channel=self.name,
                    chat_id=chat_id,
                    content=f"✅ {action}。",
                    metadata=meta,
                ))
            return

        if hasattr(self, "_handle_message"):
            await self._handle_message(
                sender_id=sender,
                chat_id=chat_id,
                content=command,
                metadata=meta,
            )
            return

        await self.bus.publish_inbound(InboundMessage(
            channel=self.name,
            sender_id=sender,
            chat_id=chat_id,
            content=command,
            metadata=meta,
        ))

    async def _on_cancel(self, update: Any, context: Any) -> None:
        if not update.message or not update.effective_user:
            return
        sender = str(update.effective_user.id)
        if hasattr(self, "_sender_id"):
            sender = self._sender_id(update.effective_user)  # type: ignore[attr-defined]
        if not self.is_allowed(sender):
            return
        chat_id = str(update.message.chat_id)
        # /cancel is the universal "abort whatever is happening":
        # 1) exit an inline edit flow if one is pending;
        if chat_id in self._edit_state:
            del self._edit_state[chat_id]
            await update.message.reply_text("❌ 已取消编辑")
            return
        # 2) otherwise stop a running group-chat task (so /cancel and /stop
        #    are no longer two subtly-different "stop" commands);
        engine = getattr(self, "_groupchat_engine", None)
        if engine and getattr(engine, "_running", False):
            engine.stop()
            await update.message.reply_text("✅ 已停止当前任务。")
            return
        # 3) nothing to cancel.
        await update.message.reply_text("ℹ️ 当前没有进行中的交互操作。")
