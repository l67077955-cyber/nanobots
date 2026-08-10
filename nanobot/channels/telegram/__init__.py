"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from loguru import logger
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import TelegramConfig
from nanobot.i18n import i18n
import nanobot.i18n_catalog  # noqa: F401  (registers UI strings)
from nanobot.groupchat.orchestra.engine import GroupChatEngine
from nanobot.groupchat.display import display as _d
from nanobot.groupchat.history.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
)
from nanobot.utils.helpers import split_message

from .formatting import (
    TELEGRAM_MAX_MESSAGE_LEN,
    _strip_md,
    _render_table_box,
    _markdown_to_telegram_html,
)
from .callbacks import CallbacksMixin
from .message_handler import MessageHandlerMixin
from .commands.agents import AgentCommandsMixin
from .commands.providers import ProviderCommandsMixin
from .commands.settings import SettingsCommandsMixin
from .commands.groups import GroupCommandsMixin
from .commands.log import LogCommandsMixin

# Re-export for backward compatibility
__all__ = ["TelegramChannel", "TELEGRAM_MAX_MESSAGE_LEN"]


class TelegramChannel(
    AgentCommandsMixin,
    ProviderCommandsMixin,
    SettingsCommandsMixin,
    GroupCommandsMixin,
    LogCommandsMixin,
    CallbacksMixin,
    MessageHandlerMixin,
    BaseChannel,
):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

    name = "telegram"

    BOT_COMMANDS = [
        BotCommand("new", "Start a new conversation"),
        BotCommand("clear", "Clear conversation history"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("cancel", "Cancel current interaction"),
        BotCommand("agents", "List available agents"),
        BotCommand("addagent", "Add agent to chat"),
        BotCommand("removeagent", "Remove agent"),
        BotCommand("newagent", "Create new agent"),
        BotCommand("editagent", "Edit agent config"),
        BotCommand("hyperparams", "View/edit sampling params"),
        BotCommand("restart", "Hard reset system"),
        BotCommand("log", "View session log"),
        BotCommand("savegroup", "Save current members as group"),
        BotCommand("loadgroup", "Load saved group"),
        BotCommand("delgroup", "Delete saved group"),
        BotCommand("groups", "List saved groups"),
        BotCommand("order", "Change agent speaking order"),
        BotCommand("setleader", "Set/clear leader agent"),
        BotCommand("settings", "⚙️ 配置:语言/回复/群聊策略"),
        BotCommand("providers", "查看提供商和模型"),
        BotCommand("newprovider", "添加提供商"),
        BotCommand("newmodel", "添加模型"),
        BotCommand("editprovider", "编辑提供商"),
        BotCommand("deleteprovider", "删除提供商"),
        BotCommand("deletemodel", "删除模型"),
        BotCommand("speedtest", "提供商测速"),
        BotCommand("prompt", "查看/编辑/排序提示词"),
        BotCommand("history", "历史管理流程/设置"),
        BotCommand("groupchat", "群聊参数设置"),
        BotCommand("help", "Show commands"),
    ]

    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        groq_api_key: str = "",
    ):
        # Convert raw dict to TelegramConfig model if needed
        if isinstance(config, dict):
            config = TelegramConfig(**config)
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.groq_api_key = groq_api_key
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        self._media_group_buffers: dict[str, dict] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._message_threads: dict[tuple[str, int], int] = {}
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None
        # Group chat engine (initialized in set_groupchat_engine)
        self._groupchat_engine: GroupChatEngine | None = None
        # Edit state for interactive /editagent flow
        self._edit_state: dict[str, dict] = {}  # chat_id -> {agent, field}
        # tracks when a chat last had an active interactive edit, so stale
        # edit-sessions don't swallow later normal messages
        self._edit_state_since: dict[str, float] = {}
        # pending confirmed-input buffer: chat_id -> {"content": str, "ts": float}
        # A typed message is NOT consumed directly; it is staged here and only
        # applied when the user taps the confirm button (inpc_confirm). Expires
        # after EDIT_CONFIRM_TIMEOUT seconds.
        self._pending_input: dict[str, dict] = {}

    def set_groupchat_engine(self, engine: GroupChatEngine) -> None:
        """Set the group chat engine for multi-agent discussions."""
        self._groupchat_engine = engine
        # Load persisted retry config from pm.json
        try:
            pm = self._load_pm()
            for _name, info in pm.get("providers", {}).items():
                delays = info.get("retryDelays")
                if delays and hasattr(engine, "provider"):
                    engine.provider._retry_delays = tuple(delays)
                    logger.info("Loaded retry delays from pm.json: {}", delays)
                    break
        except Exception:
            pass
        logger.info("Telegram: group chat engine set with {} agents", len(engine.registry))

    def is_allowed(self, sender_id: str) -> bool:
        """Preserve Telegram's legacy id|username allowlist matching."""
        if super().is_allowed(sender_id):
            return True

        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list or "*" in allow_list:
            return False

        sender_str = str(sender_id)
        if sender_str.count("|") != 1:
            return False

        sid, username = sender_str.split("|", 1)
        if not sid.isdigit() or not username:
            return False

        return sid in allow_list or username in allow_list

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True

        # Build the application with larger connection pool to avoid pool-timeout on long runs
        req = HTTPXRequest(
            connection_pool_size=16,
            pool_timeout=5.0,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=self.config.proxy if self.config.proxy else None,
        )
        builder = Application.builder().token(self.config.token).request(req).get_updates_request(req)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Add command handlers
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("new", self._forward_command))
        self._app.add_handler(CommandHandler("clear", self._forward_command))
        self._app.add_handler(CommandHandler("stop", self._forward_command))
        self._app.add_handler(CommandHandler("cancel", self._on_cancel))
        self._app.add_handler(CommandHandler("help", self._on_help))
        self._app.add_handler(CommandHandler("settings", self._on_settings))
        # Agent management commands
        self._app.add_handler(CommandHandler("agents", self._on_agents))
        self._app.add_handler(CommandHandler("addagent", self._on_addagent))
        self._app.add_handler(CommandHandler("removeagent", self._on_removeagent))
        self._app.add_handler(CommandHandler("newagent", self._on_newagent))
        self._app.add_handler(CommandHandler("editagent", self._on_editagent))
        self._app.add_handler(CommandHandler("hyperparams", self._on_hyperparams))
        self._app.add_handler(CommandHandler("restart", self._on_restart))
        self._app.add_handler(CommandHandler("log", self._on_log))
        self._app.add_handler(CommandHandler("savegroup", self._on_savegroup))
        self._app.add_handler(CommandHandler("loadgroup", self._on_loadgroup))
        self._app.add_handler(CommandHandler("delgroup", self._on_delgroup))
        self._app.add_handler(CommandHandler("groups", self._on_groups))
        self._app.add_handler(CommandHandler("order", self._on_order))
        self._app.add_handler(CommandHandler("setleader", self._on_setleader))
        self._app.add_handler(CommandHandler("prompt", self._on_prompt))
        self._app.add_handler(CommandHandler("history", self._on_history))
        # Provider & model management
        self._app.add_handler(CommandHandler("newprovider", self._on_newprovider))
        self._app.add_handler(CommandHandler("newmodel", self._on_newmodel))
        self._app.add_handler(CommandHandler("deleteprovider", self._on_deleteprovider))
        self._app.add_handler(CommandHandler("deletemodel", self._on_deletemodel))
        self._app.add_handler(CommandHandler("editprovider", self._on_editprovider))
        self._app.add_handler(CommandHandler("providers", self._on_providers))
        self._app.add_handler(CommandHandler("speedtest", self._on_speedtest))
        self._app.add_handler(CommandHandler("groupchat", self._on_groupchat))
        self._app.add_handler(CallbackQueryHandler(self._on_callback))

        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL)
                & ~filters.COMMAND,
                self._on_message
            )
        )

        logger.info("Starting Telegram bot (polling mode)...")

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()

        # Get bot info and register command menu
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        logger.info("Telegram bot @{} connected", bot_info.username)

        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            logger.debug("Telegram bot commands registered")
        except Exception as e:
            logger.warning("Failed to register bot commands: {}", e)

        # Start polling (this runs until stopped)
        await self._app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True  # Ignore old messages on startup
        )

        # Check for post-restart notification
        restart_file = Path("/tmp/nanobot_restart.json")
        if restart_file.exists():
            try:
                import json as _json
                info = _json.loads(restart_file.read_text())
                chat_id = info.get("chat_id")
                ts = info.get("ts", "?")
                if chat_id:
                    import time as _time
                    boot_time = _time.strftime("%H:%M:%S")
                    await self._app.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"✅ 重启完成\n请求时间: {ts}\n启动时间: {boot_time}",
                    )
            except Exception as e:
                logger.warning("Failed to send restart notification: {}", e)
            finally:
                restart_file.unlink(missing_ok=True)

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False

        # Cancel all typing indicators
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        for task in self._media_group_tasks.values():
            task.cancel()
        self._media_group_tasks.clear()
        self._media_group_buffers.clear()

        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None

    @staticmethod
    def _get_media_type(path: str) -> str:
        """Guess media type from file extension."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "photo"
        if ext == "ogg":
            return "voice"
        if ext in ("mp3", "m4a", "wav", "aac"):
            return "audio"
        return "document"

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        # Only stop typing indicator for final responses
        if not msg.metadata.get("_progress", False):
            self._stop_typing(msg.chat_id)

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            logger.error("Invalid chat_id: {}", msg.chat_id)
            return
        reply_to_message_id = msg.metadata.get("message_id")
        message_thread_id = msg.metadata.get("message_thread_id")
        if message_thread_id is None and reply_to_message_id is not None:
            message_thread_id = self._message_threads.get((msg.chat_id, reply_to_message_id))
        thread_kwargs = {}
        if message_thread_id is not None:
            thread_kwargs["message_thread_id"] = message_thread_id

        reply_params = None
        if self.config.reply_to_message:
            if reply_to_message_id:
                reply_params = ReplyParameters(
                    message_id=reply_to_message_id,
                    allow_sending_without_reply=True
                )

        # Send media files
        for media_path in (msg.media or []):
            try:
                media_type = self._get_media_type(media_path)
                sender = {
                    "photo": self._app.bot.send_photo,
                    "voice": self._app.bot.send_voice,
                    "audio": self._app.bot.send_audio,
                }.get(media_type, self._app.bot.send_document)
                param = "photo" if media_type == "photo" else media_type if media_type in ("voice", "audio") else "document"
                with open(media_path, 'rb') as f:
                    await sender(
                        chat_id=chat_id,
                        **{param: f},
                        reply_parameters=reply_params,
                        **thread_kwargs,
                    )
            except Exception as e:
                filename = media_path.rsplit("/", 1)[-1]
                logger.error("Failed to send media {}: {}", media_path, e)
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=f"[Failed to send: {filename}]",
                    reply_parameters=reply_params,
                    **thread_kwargs,
                )

        # Send text content
        if msg.content and msg.content != "[empty message]":
            is_progress = msg.metadata.get("_progress", False)

            for chunk in split_message(msg.content, TELEGRAM_MAX_MESSAGE_LEN):
                # Final response: simulate streaming via draft, then persist
                if not is_progress:
                    await self._send_with_streaming(chat_id, chunk, reply_params, thread_kwargs)
                else:
                    await self._send_text(chat_id, chunk, reply_params, thread_kwargs)

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        reply_params=None,
        thread_kwargs: dict | None = None,
    ) -> None:
        """Send a plain text message with HTML fallback."""
        try:
            html = _markdown_to_telegram_html(text)
            await self._app.bot.send_message(
                chat_id=chat_id, text=html, parse_mode="HTML",
                reply_parameters=reply_params,
                **(thread_kwargs or {}),
            )
        except Exception as e:
            logger.warning("HTML parse failed, falling back to plain text: {}", e)
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_parameters=reply_params,
                    **(thread_kwargs or {}),
                )
            except Exception as e2:
                logger.error("Error sending Telegram message: {}", e2)

    async def _send_with_streaming(
        self,
        chat_id: int,
        text: str,
        reply_params=None,
        thread_kwargs: dict | None = None,
    ) -> None:
        """Simulate streaming via send_message_draft, then persist with send_message."""
        draft_id = int(time.time() * 1000) % (2**31)
        try:
            step = max(len(text) // 8, 40)
            for i in range(step, len(text), step):
                await self._app.bot.send_message_draft(
                    chat_id=chat_id, draft_id=draft_id, text=text[:i],
                )
                await asyncio.sleep(0.04)
            await self._app.bot.send_message_draft(
                chat_id=chat_id, draft_id=draft_id, text=text,
            )
            await asyncio.sleep(0.15)
        except Exception:
            pass
        await self._send_text(chat_id, text, reply_params, thread_kwargs)

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command, bypassing ACL so all users can access it."""
        if not update.message:
            return
        await update.message.reply_text(
            "🐈 nanobot commands:\n"
            "/new — 新对话\n"
            "/clear — 清空上下文\n"
            "/stop — 停止当前任务\n"
            "/cancel — 取消交互操作\n\n"
            "🎭 Agent 管理:\n"
            "/agents — 查看所有 agent\n"
            "/addagent <name> — 加入 agent\n"
            "/removeagent <name> — 移除 agent\n"
            "/newagent <name> — 创建新 agent\n"
            "/editagent <name> — 编辑 agent (名字/人设/模型/工具)\n"
            "/hyperparams — 查看/修改超参数\n"
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

    # ── Config settings (/settings) ─────────────────────────────
    async def _on_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Edit bot configuration. First screen is the edit panel itself —
        not a routing hub. Each row is one editable setting (button), tapping
        it opens the choice/toggle for that setting. Persists to config.yaml.
        """
        if not update.message:
            return
        # Locale follows the bot's configured language.
        if getattr(self, "config", None) and getattr(self.config, "language", None):
            i18n.set_locale(self.config.language)
        cfg = self.config
        lang = getattr(cfg, "language", "zh")
        rtm = getattr(cfg, "reply_to_message", True)
        gp = getattr(cfg, "group_policy", "mention")
        buttons = [
            [InlineKeyboardButton(i18n.t("ui.config.language", v=lang), callback_data="m:cfg:language")],
            [InlineKeyboardButton(i18n.t("ui.config.reply_to_message", v="✓" if rtm else "✗"), callback_data="m:cfg:reply_to_message")],
            [InlineKeyboardButton(i18n.t("ui.config.group_policy", v=gp), callback_data="m:cfg:group_policy")],
            [InlineKeyboardButton(i18n.t("ui.common.cancel"), callback_data="m:cfg:cancel")],  # root → cancel
        ]
        await update.message.reply_text(
            i18n.t("ui.config.title"),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Agent Management Commands ────────────────────────────

    # ── Forward command ──
    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward /new, /clear and /stop as inbound messages."""
        if not update.message or not update.effective_user:
            return
        sender = self._sender_id(update.effective_user)
        if not self.is_allowed(sender):
            return
        chat_id = str(update.message.chat_id)
        command = update.message.text or ""
        # Extract bare command name (strip @botname suffix)
        cmd = command.strip().split()[0].lower().split("@")[0]

        # All commands handled by GroupChatEngine — no forwarding to AgentLoop bus
        if self._groupchat_engine:
            if cmd == "/stop":
                was_running = self._groupchat_engine._running
                self._groupchat_engine.stop()
                msg = "✅ 群聊已停止。" if was_running else "ℹ️ 当前没有运行中的任务。"
                from nanobot.bus.events import OutboundMessage
                await self.bus.publish_outbound(OutboundMessage(
                    channel="telegram",
                    chat_id=chat_id,
                    content=msg,
                    metadata={
                        "message_id": update.message.message_id,
                        "message_thread_id": update.message.message_thread_id,
                    },
                ))
            elif cmd in ("/clear", "/new"):
                self._groupchat_engine.clear_history()
                action = "新对话已开始" if cmd == "/new" else "上下文已清空"
                from nanobot.bus.events import OutboundMessage
                await self.bus.publish_outbound(OutboundMessage(
                    channel="telegram",
                    chat_id=chat_id,
                    content=f"✅ {action}。",
                    metadata={
                        "message_id": update.message.message_id,
                        "message_thread_id": update.message.message_thread_id,
                    },
                ))
            return

        # Fallback: no groupchat engine configured (shouldn't happen in normal operation)
        from nanobot.bus.events import InboundMessage
        await self.bus.publish_inbound(InboundMessage(
            channel="telegram",
            sender_id=sender,
            chat_id=chat_id,
            content=command,
            metadata={
                "message_id": update.message.message_id,
                "message_thread_id": update.message.message_thread_id,
            },
        ))

    async def _on_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cancel command to abort interactive edits."""
        if not update.message or not update.effective_user:
            return
        sender = self._sender_id(update.effective_user)
        if not self.is_allowed(sender):
            return
        chat_id = str(update.message.chat_id)
        if chat_id in self._edit_state:
            del self._edit_state[chat_id]
            await update.message.reply_text("❌ 已取消")
        else:
            await update.message.reply_text("ℹ️ 当前没有进行中的交互操作。")
