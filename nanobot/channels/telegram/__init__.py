"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import suppress
from pathlib import Path

from loguru import logger
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.error import TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.commands_core import CoreCommandsMixin
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import TelegramConfig
from nanobot.groupchat.runtime.engine import GroupChatEngine
from nanobot.groupchat.display import display as _d
from nanobot.groupchat.context.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
)
from nanobot.runtime.dispatch import InboundDispatcher
from nanobot.security.network import validate_url_target
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

_SEND_RETRY_BASE_DELAY = 1.0
_SEND_RETRY_MAX_ATTEMPTS = 3


class TelegramChannel(
    CoreCommandsMixin,
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

    @classmethod
    def default_config(cls) -> dict:
        return {"enabled": False, **TelegramConfig().model_dump(by_alias=True)}

    BOT_COMMANDS = [
        BotCommand("start", "Show welcome message"),
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
        BotCommand("think", "Set agent thinking depth"),
        BotCommand("restart", "Hard reset system"),
        BotCommand("log", "View session log"),
        BotCommand("summary", "Generate conversation summary"),
        BotCommand("savegroup", "Save current members as group"),
        BotCommand("loadgroup", "Load saved group"),
        BotCommand("delgroup", "Delete saved group"),
        BotCommand("groups", "List saved groups"),
        BotCommand("order", "Change agent speaking order"),
        BotCommand("setleader", "Set/clear leader agent"),
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
        self._dispatcher = InboundDispatcher()

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
        """Support legacy composite "id|username" (from _sender_id mixin) for allow_from.

        Base class already does the right thing for:
          - empty allow_from → deny all (with warning)
          - "*" in list → allow everyone
          - exact string match on the passed sender_id value

        This override adds tolerance so users can put either the raw numeric ID
        *or* the @username into allowFrom, even when the runtime value passed
        to is_allowed is the composite form "123456|alice" (or plain "123456"
        when the user has no username set).
        """
        if super().is_allowed(sender_id):
            return True

        allow_list = getattr(self.config, "allow_from", []) or []
        sender_str = str(sender_id or "")

        # Composite form produced by the mixin when username is present
        if "|" in sender_str:
            try:
                sid, username = sender_str.split("|", 1)
                if not sid.isdigit():
                    return False
                if sid in allow_list or (username and username in allow_list):
                    return True
            except ValueError:
                pass

        # Plain ID (no username) or the whole string listed
        if sender_str in allow_list:
            return True

        return False

    def _interrupt_before_command(self) -> None:
        """Abort in-flight agent streaming before any slash command or button."""
        if self._groupchat_engine:
            self._groupchat_engine.interrupt_active_turn()

    def _wrap_command(self, handler):
        """Wrap a command handler to cleanly interrupt active agent turns first."""
        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            self._interrupt_before_command()
            await handler(update, context)
        return wrapped

    def _register_commands(self) -> None:
        """Register per-command handlers (stable UX + interrupt-safe)."""
        commands = (
            ("start", self._on_start),
            ("new", self._forward_command),
            ("clear", self._forward_command),
            ("stop", self._forward_command),
            ("cancel", self._on_cancel),
            ("help", self._on_help),
            ("agents", self._on_agents),
            ("addagent", self._on_addagent),
            ("removeagent", self._on_removeagent),
            ("newagent", self._on_newagent),
            ("editagent", self._on_editagent),
            ("hyperparams", self._on_hyperparams),
            ("think", self._on_think),
            ("restart", self._on_restart),
            ("log", self._on_log),
            ("summary", self._on_summary),
            ("savegroup", self._on_savegroup),
            ("loadgroup", self._on_loadgroup),
            ("delgroup", self._on_delgroup),
            ("groups", self._on_groups),
            ("order", self._on_order),
            ("setleader", self._on_setleader),
            ("prompt", self._on_prompt),
            ("history", self._on_history),
            ("newprovider", self._on_newprovider),
            ("newmodel", self._on_newmodel),
            ("deleteprovider", self._on_deleteprovider),
            ("deletemodel", self._on_deletemodel),
            ("editprovider", self._on_editprovider),
            ("providers", self._on_providers),
            ("speedtest", self._on_speedtest),
            ("groupchat", self._on_groupchat),
        )
        for name, handler in commands:
            self._app.add_handler(CommandHandler(name, self._wrap_command(handler)))

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True

        # Use separate pools for API calls and getUpdates long polling.
        api_req = HTTPXRequest(
            connection_pool_size=self.config.connection_pool_size,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            proxy=self.config.proxy if self.config.proxy else None,
        )
        poll_req = HTTPXRequest(
            connection_pool_size=self.config.get_updates_connection_pool_size,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=self.config.connect_timeout,
            read_timeout=self.config.read_timeout,
            proxy=self.config.proxy if self.config.proxy else None,
        )
        builder = Application.builder().token(self.config.token).request(api_req).get_updates_request(poll_req)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        self._register_commands()
        self._app.add_handler(CallbackQueryHandler(self._wrap_command(self._on_callback)))

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
                if chat_id and str(chat_id).isdigit():
                    import time as _time
                    boot_time = _time.strftime("%H:%M:%S")
                    elapsed = ""
                    started_at = info.get("started_at")
                    if started_at:
                        with suppress(ValueError, TypeError):
                            elapsed = f"\n耗时: {max(0.0, _time.time() - float(started_at)):.1f}s"
                    await self._app.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"✅ 重启完成\n请求时间: {ts}\n启动时间: {boot_time}{elapsed}",
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
                if str(media_path).startswith(("http://", "https://")):
                    ok, err = validate_url_target(str(media_path))
                    if not ok:
                        filename = str(media_path).rstrip("/").rsplit("/", 1)[-1] or "media"
                        logger.warning("Blocked Telegram remote media URL {}: {}", media_path, err)
                        await self._app.bot.send_message(
                            chat_id=chat_id,
                            text=f"[Failed to send: {filename}]",
                            reply_parameters=reply_params,
                            **thread_kwargs,
                        )
                        continue
                    media_type = self._get_media_type(str(media_path))
                    sender = {
                        "photo": self._app.bot.send_photo,
                        "voice": self._app.bot.send_voice,
                        "audio": self._app.bot.send_audio,
                    }.get(media_type, self._app.bot.send_document)
                    param = "photo" if media_type == "photo" else media_type if media_type in ("voice", "audio") else "document"
                    await sender(
                        chat_id=chat_id,
                        **{param: str(media_path)},
                        reply_parameters=reply_params,
                        **thread_kwargs,
                    )
                    continue
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
        """Send a plain text message with HTML fallback and timeout retries."""
        last_error = None
        for attempt in range(_SEND_RETRY_MAX_ATTEMPTS):
            try:
                html = _markdown_to_telegram_html(text)
                await self._app.bot.send_message(
                    chat_id=chat_id, text=html, parse_mode="HTML",
                    reply_parameters=reply_params,
                    **(thread_kwargs or {}),
                )
                return
            except TimedOut as e:
                last_error = e
                if attempt < _SEND_RETRY_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_SEND_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                logger.error("Error sending Telegram message after retries: {}", e)
                return
            except Exception as e:
                logger.warning("HTML parse failed, falling back to plain text: {}", e)
                break

        for attempt in range(_SEND_RETRY_MAX_ATTEMPTS):
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_parameters=reply_params,
                    **(thread_kwargs or {}),
                )
                return
            except TimedOut as e:
                last_error = e
                if attempt < _SEND_RETRY_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_SEND_RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                logger.error("Error sending Telegram message after retries: {}", e)
                return
            except Exception as e2:
                last_error = e2
                logger.error("Error sending Telegram message: {}", e2)
                break
        if last_error:
            logger.error("Error sending Telegram message: {}", last_error)

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
