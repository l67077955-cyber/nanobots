"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
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
from nanobot.groupchat.engine import GroupChatEngine
from nanobot.utils.helpers import split_message

TELEGRAM_MAX_MESSAGE_LEN = 4000  # Telegram message character limit


def _strip_md(s: str) -> str:
    """Strip markdown inline formatting from text."""
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'__(.+?)__', r'\1', s)
    s = re.sub(r'~~(.+?)~~', r'\1', s)
    s = re.sub(r'`([^`]+)`', r'\1', s)
    return s.strip()


def _render_table_box(table_lines: list[str]) -> str:
    """Convert markdown pipe-table to compact aligned text for <pre> display."""

    def dw(s: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)

    rows: list[list[str]] = []
    has_sep = False
    for line in table_lines:
        cells = [_strip_md(c) for c in line.strip().strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
            has_sep = True
            continue
        rows.append(cells)
    if not rows or not has_sep:
        return '\n'.join(table_lines)

    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([''] * (ncols - len(r)))
    widths = [max(dw(r[c]) for r in rows) for c in range(ncols)]

    def dr(cells: list[str]) -> str:
        return '  '.join(f'{c}{" " * (w - dw(c))}' for c, w in zip(cells, widths))

    out = [dr(rows[0])]
    out.append('  '.join('─' * w for w in widths))
    for row in rows[1:]:
        out.append(dr(row))
    return '\n'.join(out)


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)

    # 1.5. Convert markdown tables to box-drawing (reuse code_block placeholders)
    lines = text.split('\n')
    rebuilt: list[str] = []
    li = 0
    while li < len(lines):
        if re.match(r'^\s*\|.+\|', lines[li]):
            tbl: list[str] = []
            while li < len(lines) and re.match(r'^\s*\|.+\|', lines[li]):
                tbl.append(lines[li])
                li += 1
            box = _render_table_box(tbl)
            if box != '\n'.join(tbl):
                code_blocks.append(box)
                rebuilt.append(f"\x00CB{len(code_blocks) - 1}\x00")
            else:
                rebuilt.extend(tbl)
        else:
            rebuilt.append(lines[li])
            li += 1
    text = '\n'.join(rebuilt)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)

    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

    name = "telegram"

    # Commands registered with Telegram's command menu
    BOT_COMMANDS = [
        BotCommand("new", "Start a new conversation"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("agents", "List available agents"),
        BotCommand("addagent", "Add agent to chat"),
        BotCommand("removeagent", "Remove agent"),
        BotCommand("newagent", "Create new agent"),
        BotCommand("editagent", "Edit agent config"),
        BotCommand("hyperparams", "View/edit sampling params"),
        BotCommand("endchat", "Clear all agents"),
        BotCommand("restart", "Hard reset system"),
        BotCommand("log", "View session log"),
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
        BotCommand("help", "Show commands"),
    ]

    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        groq_api_key: str = "",
    ):
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

    def set_groupchat_engine(self, engine: GroupChatEngine) -> None:
        """Set the group chat engine for multi-agent discussions."""
        self._groupchat_engine = engine
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
        self._app.add_handler(CommandHandler("stop", self._forward_command))
        self._app.add_handler(CommandHandler("help", self._on_help))
        # Agent management commands
        self._app.add_handler(CommandHandler("agents", self._on_agents))
        self._app.add_handler(CommandHandler("addagent", self._on_addagent))
        self._app.add_handler(CommandHandler("removeagent", self._on_removeagent))
        self._app.add_handler(CommandHandler("newagent", self._on_newagent))
        self._app.add_handler(CommandHandler("editagent", self._on_editagent))
        self._app.add_handler(CommandHandler("hyperparams", self._on_hyperparams))
        self._app.add_handler(CommandHandler("endchat", self._on_endchat))
        self._app.add_handler(CommandHandler("restart", self._on_restart))
        self._app.add_handler(CommandHandler("log", self._on_log))
        self._app.add_handler(CommandHandler("savegroup", self._on_savegroup))
        self._app.add_handler(CommandHandler("loadgroup", self._on_loadgroup))
        self._app.add_handler(CommandHandler("delgroup", self._on_delgroup))
        self._app.add_handler(CommandHandler("groups", self._on_groups))
        self._app.add_handler(CommandHandler("order", self._on_order))
        self._app.add_handler(CommandHandler("setleader", self._on_setleader))
        # Provider & model management
        self._app.add_handler(CommandHandler("newprovider", self._on_newprovider))
        self._app.add_handler(CommandHandler("newmodel", self._on_newmodel))
        self._app.add_handler(CommandHandler("deleteprovider", self._on_deleteprovider))
        self._app.add_handler(CommandHandler("deletemodel", self._on_deletemodel))
        self._app.add_handler(CommandHandler("editprovider", self._on_editprovider))
        self._app.add_handler(CommandHandler("providers", self._on_providers))
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
            "/stop — 停止当前任务\n\n"
            "🎭 Agent 管理:\n"
            "/agents — 查看所有 agent\n"
            "/addagent <name> — 加入 agent\n"
            "/removeagent <name> — 移除 agent\n"
            "/newagent <name> — 创建新 agent\n"
            "/editagent <name> — 编辑 agent (名字/人设/模型/工具)\n"
            "/hyperparams — 查看/修改超参数\n"
            "/endchat — 清空所有 agent\n"
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
            "/deletemodel — 删除模型\n\n"
            "📊 日志 & 调试：\n"
            "/log — 查看 LLM 调用记录 (tokens/延迟/工具)\n"
            "/summary — 生成对话总结\n\n"
            "💡 加入 agent 后直接发消息即可对话\n"
            "2+ agent 自动进入群聊模式"
        )

    # ── Agent Management Commands ────────────────────────────

    def _ensure_gc_send(self, chat_id: str) -> None:
        """Ensure the group chat engine has a send callback for this chat."""
        if self._groupchat_engine and not self._groupchat_engine._send_fn:
            async def send_fn(text: str) -> None:
                await self._gc_send(chat_id, text)
            self._groupchat_engine.set_send_fn(send_fn)

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
            leader = " 👑" if self._groupchat_engine.leader == name else ""
            model = info.get("model", "?")
            # Tools summary
            tools_cfg = info.get("tools")
            if isinstance(tools_cfg, dict):
                on = [k for k, v in tools_cfg.items() if v]
                tools_str = ", ".join(on) if on else "无"
            elif info.get("tools_enabled", False):
                tools_str = "全部"
            else:
                tools_str = "无"
            # Persona preview
            prompt = info.get("prompt", "")
            persona = prompt[:60].replace("\n", " ") + "…" if len(prompt) > 60 else prompt.replace("\n", " ")
            # Find provider for this model
            prov_name = "默认"
            for pn, model_list in pm.get("models", {}).items():
                if model in model_list:
                    prov_name = pn
                    break
            lines.append(f"{status} {name}{leader}")
            lines.append(f"   🤖 {model}")
            lines.append(f"   🏢 {prov_name}")
            lines.append(f"   🔧 {tools_str}")
            lines.append(f"   📝 {persona}")
            lines.append("")
        if active:
            order = " → ".join(active)
            lines.append(f"👥 发言顺序: {order}")
        else:
            lines.append("💤 无活跃 agent")
        text = "\n".join(lines)
        await update.message.reply_text(text[:4096])

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
        if not args:
            # Clear leader or show current
            if self._groupchat_engine.leader:
                result = self._groupchat_engine.set_leader(None)
            else:
                # Show selection buttons
                buttons = []
                for name in self._groupchat_engine.registry:
                    buttons.append([InlineKeyboardButton(
                        f"👑 {name}",
                        callback_data=f"sl:{name}"
                    )])
                await update.message.reply_text(
                    "👑 选择 Leader:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return
        else:
            result = self._groupchat_engine.set_leader(args[0])
        await update.message.reply_text(result)

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
        buttons = [[InlineKeyboardButton(f"{n} ({i.get('model','?')})", callback_data=f"add:{n}")] for n, i in available]
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
        if name and self._groupchat_engine._resolve_agent_name(name):
            await update.message.reply_text(f"⚠️ Agent '{name}' 已存在，用 /editagent 修改")
            return
        chat_id = str(update.message.chat_id)
        if name:
            self._edit_state[chat_id] = {"agent": name, "field": "create_model", "mode": "create"}
            await update.message.reply_text(
                f"🆕 创建 Agent: {name}\n\n"
                "请输入模型名:\n"
                "(如 anthropic/claude-sonnet-4-5, x-ai/grok-4.1-fast)"
            )
        else:
            self._edit_state[chat_id] = {"agent": "", "field": "create_name", "mode": "create"}
            await update.message.reply_text("🆕 创建新 Agent\n\n请输入 Agent 名字:")

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
        matched = self._groupchat_engine._resolve_agent_name(name)
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
        return (
            f"✏️ 编辑 {agent_name}\n\n"
            f"模型: {agent['model']}\n"
            f"工具: {tools_str}\n"
            f"人设: {agent['prompt'][:100]}..."
        )

    def _edit_menu_buttons(self, agent_name: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ 修改名字", callback_data=f"ef:{agent_name}:name")],
            [InlineKeyboardButton("📝 修改人设", callback_data=f"ef:{agent_name}:persona")],
            [InlineKeyboardButton("🤖 修改模型", callback_data=f"ef:{agent_name}:model")],
            [InlineKeyboardButton("🔧 工具设置", callback_data=f"ef:{agent_name}:tools")],
            [InlineKeyboardButton("❌ 取消", callback_data=f"ef:{agent_name}:cancel")],
        ])

    async def _show_edit_menu(self, update_or_query, agent_name: str) -> None:
        """Show edit menu for an agent."""
        if hasattr(update_or_query, 'message') and update_or_query.message:
            chat_id = str(update_or_query.message.chat_id)
            await update_or_query.message.reply_text(
                self._edit_menu_text(agent_name),
                reply_markup=self._edit_menu_buttons(agent_name),
            )
        else:
            return

    # ── Provider / Model management ─────────────────────────────────────

    def _pm_path(self) -> Path:
        return Path.home() / ".nanobot" / "providers_models.json"

    def _load_pm(self) -> dict:
        p = self._pm_path()
        if p.exists():
            return json.loads(p.read_text())
        return {"providers": {}, "models": {}}

    def _save_pm(self, data: dict) -> None:
        self._pm_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))

    async def _on_newprovider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start flow: name → URL → apiKey."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        chat_id = str(update.message.chat_id)
        self._edit_state[chat_id] = {"field": "pm_prov_name", "mode": "pm"}
        await update.message.reply_text("🆕 创建提供商\n\n请输入提供商名称 (如 openrouter, aihubmix):")

    async def _on_newmodel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show provider keyboard, then ask for model ID."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = list(pm.get("providers", {}).keys())
        if not provs:
            await update.message.reply_text("⚠️ 还没有提供商，请先 /newprovider")
            return
        buttons = [[InlineKeyboardButton(f"🏢 {p}", callback_data=f"pm_newm:{p}")] for p in provs]
        await update.message.reply_text("🆕 添加模型\n\n选择提供商:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_deleteprovider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = list(pm.get("providers", {}).keys())
        if not provs:
            await update.message.reply_text("⚠️ 没有提供商")
            return
        buttons = [[InlineKeyboardButton(f"🗑 {p}", callback_data=f"pm_delp:{p}")] for p in provs]
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        await update.message.reply_text("🗑 删除提供商\n\n选择要删除的:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_deletemodel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = [p for p in pm.get("providers", {}) if pm.get("models", {}).get(p)]
        if not provs:
            await update.message.reply_text("⚠️ 没有可删除的模型")
            return
        buttons = [[InlineKeyboardButton(f"🏢 {p} ({len(pm['models'].get(p, []))} models)", callback_data=f"pm_delm_p:{p}")] for p in provs]
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        await update.message.reply_text("🗑 删除模型\n\n先选择提供商:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_editprovider(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Edit an existing provider's URL or API key."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = list(pm.get("providers", {}).keys())
        if not provs:
            await update.message.reply_text("⚠️ 没有提供商，请先 /newprovider")
            return
        buttons = []
        for p in provs:
            info = pm["providers"][p]
            url = info.get("url", "?")
            key_preview = info.get("apiKey", "")[:8] + "..." if info.get("apiKey") else "(none)"
            buttons.append([InlineKeyboardButton(f"✏️ {p} ({url})", callback_data=f"ep_pick:{p}")])
        buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
        await update.message.reply_text("✏️ 编辑提供商\n\n选择要编辑的:", reply_markup=InlineKeyboardMarkup(buttons))

    async def _on_providers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List all providers and their models."""
        if not update.message or not self.is_allowed(self._sender_id(update.effective_user)):
            return
        pm = self._load_pm()
        provs = pm.get("providers", {})
        models = pm.get("models", {})
        if not provs:
            await update.message.reply_text("📭 暂无提供商\n\n用 /newprovider 添加")
            return
        lines = ["📋 **提供商 & 模型列表**\n"]
        for name, info in provs.items():
            url = info.get("url", "?")
            key = info.get("apiKey", "")
            key_preview = key[:8] + "..." if key else "(未设置)"
            ms = models.get(name, [])
            lines.append(f"🏢 **{name}**")
            lines.append(f"   🔗 {url}")
            lines.append(f"   🔑 {key_preview}")
            if ms:
                for m in ms:
                    lines.append(f"   🤖 {m}")
            else:
                lines.append("   (无模型，用 /newmodel 添加)")
            lines.append("")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle InlineKeyboard button presses."""
        query = update.callback_query
        if not query or not query.data:
            return
        await query.answer()

        data = query.data
        chat_id = str(query.message.chat_id)

        if data.startswith("add:"):
            name = data[4:]
            self._ensure_gc_send(chat_id)
            result = self._groupchat_engine.add_agent(name)
            await query.edit_message_text(result)

        elif data.startswith("rm:"):
            name = data[3:]
            result = self._groupchat_engine.remove_agent(name)
            await query.edit_message_text(result)

        elif data.startswith("edit:"):
            name = data[5:]
            agent = self._groupchat_engine.registry.get(name)
            if not agent:
                await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                return
            await query.edit_message_text(
                self._edit_menu_text(name),
                reply_markup=self._edit_menu_buttons(name),
            )

        elif data.startswith("ef:"):
            # ef:AgentName:field
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            name, field = parts[1], parts[2]
            if field == "cancel":
                self._edit_state.pop(chat_id, None)
                await query.edit_message_text("❌ 已取消")
                return
            if field == "tools":
                # Show per-tool toggle buttons
                from nanobot.groupchat.engine import GroupChatEngine
                agent = self._groupchat_engine.registry.get(name, {})
                tools_cfg = agent.get("tools")
                # Migrate legacy tools_enabled to granular dict
                if not isinstance(tools_cfg, dict):
                    all_on = agent.get("tools_enabled", False)
                    tools_cfg = {t: all_on for t in GroupChatEngine.TOOL_NAMES}
                    agent["tools"] = tools_cfg

                labels = {
                    "web_search": "🔍 网页搜索",
                    "web_fetch": "🌐 网页抓取",
                    "exec": "⚡ 执行命令",
                    "read_file": "📄 读文件",
                    "write_file": "✍️ 写文件",
                    "edit_file": "✂️ 编辑文件",
                    "list_dir": "📁 列目录",
                }
                buttons = []
                for t in GroupChatEngine.TOOL_NAMES:
                    on = tools_cfg.get(t, False)
                    icon = "✅" if on else "❌"
                    label = labels.get(t, t)
                    buttons.append([InlineKeyboardButton(
                        f"{icon} {label}",
                        callback_data=f"tf:{name}:{t}"
                    )])
                buttons.append([InlineKeyboardButton("✅ 全开", callback_data=f"tf:{name}:__all_on"),
                                InlineKeyboardButton("❌ 全关", callback_data=f"tf:{name}:__all_off")])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])
                await query.edit_message_text(
                    f"🔧 {name} 工具设置:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return
            self._edit_state[chat_id] = {"agent": name, "field": field}
            if field == "persona":
                current = self._groupchat_engine.registry.get(name, {}).get("prompt", "")
                await query.edit_message_text(f"📄 当前人设:\n\n{current[:3000]}")
                await self._gc_send(chat_id, "请输入新人设内容:")
            elif field == "model":
                # Show provider selection keyboard
                pm = self._load_pm()
                provs = list(pm.get("providers", {}).keys())
                if provs:
                    buttons = [[InlineKeyboardButton(f"🏢 {p}", callback_data=f"em_prov:{name}:{p}")] for p in provs]
                    buttons.append([InlineKeyboardButton("✏️ 手动输入", callback_data=f"em_manual:{name}")])
                    await query.edit_message_text("🤖 选择提供商:", reply_markup=InlineKeyboardMarkup(buttons))
                else:
                    await query.edit_message_text("请输入新模型名 (如 anthropic/claude-sonnet-4-5):")
            else:
                prompts = {"name": "新名字"}
                await query.edit_message_text(f"请输入{prompts.get(field, field)}:")

        elif data.startswith("log:"):
            mode = data[4:]
            engine = self._groupchat_engine
            if not engine or (not engine._history and not engine._request_log):
                await query.edit_message_text("📭 无日志")
                return
            rlog = engine._request_log
            history = engine._history
            if mode == "brief":
                # Brief: last 5 requests
                entries = rlog[-5:] if rlog else []
                lines = [f"📋 最近请求 ({len(entries)}/{len(rlog)}):\n"]
                for r in entries:
                    err = " ❌" if r.get("error") else ""
                    lines.append(f"[{r['time']}] {r['agent']} → {r['model']} | msgs:{r['msgs']} reply:{r['reply_len']}字{err}")
                if history:
                    lines.append(f"\n💬 对话: {len(history)} 条")
                await query.edit_message_text("\n".join(lines))
            else:
                # Full: all requests + chat
                lines = [f"📜 完整日志 ({len(rlog)} 请求, {len(history)} 对话):\n"]
                lines.append("── 请求记录 ──")
                for i, r in enumerate(rlog, 1):
                    err = f" | ❌ {r['error'][:50]}" if r.get("error") else ""
                    lines.append(f"{i}. [{r['time']}] {r['mode']} | {r['agent']} → {r['model']} | msgs:{r['msgs']} max:{r['max_tokens']} reply:{r['reply_len']}字{err}")
                lines.append("\n── 对话记录 ──")
                for m in history[-10:]:
                    text = m['content'][:100] + "..." if len(m['content']) > 100 else m['content']
                    lines.append(f"[{m['sender']}]: {text}")
                full = "\n".join(lines)
                await query.edit_message_text(full[:4096])

        elif data.startswith("tf:"):
            # tf:AgentName:tool_name — toggle individual tool
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            name, tool = parts[1], parts[2]
            from nanobot.groupchat.engine import GroupChatEngine
            agent = self._groupchat_engine.registry.get(name, {})
            tools_cfg = agent.get("tools")
            if not isinstance(tools_cfg, dict):
                all_on = agent.get("tools_enabled", False)
                tools_cfg = {t: all_on for t in GroupChatEngine.TOOL_NAMES}
                agent["tools"] = tools_cfg

            if tool == "__all_on":
                for t in tools_cfg:
                    tools_cfg[t] = True
            elif tool == "__all_off":
                for t in tools_cfg:
                    tools_cfg[t] = False
            elif tool in tools_cfg:
                tools_cfg[tool] = not tools_cfg[tool]

            # Persist to config.json
            cfg_path = Path.home() / ".nanobot" / "agents" / name.lower() / "config.json"
            if cfg_path.exists():
                try:
                    cfg = json.loads(cfg_path.read_text())
                    cfg["tools"] = tools_cfg
                    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                except Exception:
                    pass

            # Refresh buttons by re-triggering tools menu
            labels = {
                "web_search": "🔍 网页搜索", "web_fetch": "🌐 网页抓取",
                "exec": "⚡ 执行命令", "read_file": "📄 读文件",
                "write_file": "✍️ 写文件", "edit_file": "✂️ 编辑文件",
                "list_dir": "📁 列目录",
            }
            buttons = []
            for t in GroupChatEngine.TOOL_NAMES:
                on = tools_cfg.get(t, False)
                icon = "✅" if on else "❌"
                label = labels.get(t, t)
                buttons.append([InlineKeyboardButton(
                    f"{icon} {label}", callback_data=f"tf:{name}:{t}"
                )])
            buttons.append([InlineKeyboardButton("✅ 全开", callback_data=f"tf:{name}:__all_on"),
                            InlineKeyboardButton("❌ 全关", callback_data=f"tf:{name}:__all_off")])
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])
            await query.edit_message_text(
                f"🔧 {name} 工具设置:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        elif data.startswith("log_pg:"):
            page = int(data[7:])
            logs = self._groupchat_engine._request_log
            text, markup = self._build_log_page(logs, page)
            await query.edit_message_text(text, reply_markup=markup)

        elif data.startswith("logd:"):
            idx = int(data[5:])
            logs = self._groupchat_engine._request_log
            if idx >= len(logs):
                await query.edit_message_text("⚠️ 记录不存在")
                return
            r = logs[idx]
            tokens = r.get("tokens", {})
            calls = r.get("calls", [])
            tools = r.get("tools", [])
            lines = [
                f"📊 LLM 调用 #{idx+1} 详情\n",
                f"👤 Agent: {r.get('agent', '?')}",
                f"🤖 Model: {r.get('model', '?')}",
                f"📌 Mode: {r.get('mode', '?')}",
                f"🕐 Time: {r.get('time', '?')} (CST)",
                f"⏱ Latency: {r.get('latency', 0)}s",
                f"🔄 Iterations: {r.get('iterations', 1)}",
                f"\n📊 Tokens:",
                f"  Prompt: {tokens.get('prompt', 0)}",
                f"  Completion: {tokens.get('completion', 0)}",
                f"  Total: {tokens.get('total', 0)}",
            ]
            if tools:
                lines.append(f"\n🔧 Tools: {', '.join(tools)}")
            if calls:
                lines.append("\n📋 Per-iteration:")
                for c in calls[:10]:
                    t = c.get("tools", [])
                    t_str = f" → {','.join(t)}" if t else ""
                    lines.append(
                        f"  i{c['iter']}: {c.get('latency',0)}s "
                        f"{c.get('tokens',{}).get('total_tokens',0)}tok "
                        f"[{c.get('finish','?')}]{t_str}"
                    )
            # Input/Output preview
            inp = r.get("input_preview", "")
            out = r.get("output", "")
            if inp:
                lines.append(f"\n📥 Input: {inp[:300]}")
            if out:
                lines.append(f"\n📤 Output: {out[:500]}")
            if r.get("error"):
                lines.append(f"\n❌ Error: {r['error'][:300]}")
            lines.append(f"\n📏 Reply: {r.get('reply_len', 0)} chars")
            text = "\n".join(lines)
            page = idx // 10
            await query.edit_message_text(
                text[:4096],
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ 返回列表", callback_data=f"log_pg:{page}")]
                ])
            )

        elif data.startswith("sl:"):
            name = data[3:]
            result = self._groupchat_engine.set_leader(name)
            await query.edit_message_text(result)

        elif data.startswith("lg:"):
            name = data[3:]
            self._ensure_gc_send(chat_id)
            result = self._groupchat_engine.load_group(name)
            await query.edit_message_text(result)

        elif data.startswith("dg:"):
            name = data[3:]
            result = self._groupchat_engine.delete_group(name)
            await query.edit_message_text(result)

        elif data.startswith("ord:"):
            val = data[4:]
            if val == "done":
                order_str = " → ".join(self._groupchat_engine.active_agents)
                await query.edit_message_text(f"📢 发言顺序:\n{order_str}")
            else:
                idx = int(val)
                agents = self._groupchat_engine.active_agents
                if 0 < idx < len(agents):
                    # Swap with previous
                    agents[idx], agents[idx-1] = agents[idx-1], agents[idx]
                    self._groupchat_engine._active_agents[:] = agents
                # Refresh keyboard
                await query.edit_message_text("📢 更新中...")
                await self._send_order_keyboard(chat_id, self._groupchat_engine.active_agents)

        # ── Provider/Model management callbacks ──
        elif data == "pm_cancel":
            self._edit_state.pop(chat_id, None)
            await query.edit_message_text("❌ 已取消")

        elif data.startswith("pm_newm:"):
            # User picked a provider for /newmodel
            prov = data[8:]
            self._edit_state[chat_id] = {"field": "pm_model_id", "mode": "pm", "provider": prov}
            await query.edit_message_text(
                f"🏢 提供商: {prov}\n\n"
                "请输入模型ID (如 google/gemini-3-flash-preview):"
            )

        elif data.startswith("pm_delp:"):
            prov = data[8:]
            pm = self._load_pm()
            model_count = len(pm.get("models", {}).get(prov, []))
            await query.edit_message_text(
                f"⚠️ 确认删除提供商 **{prov}**？\n\n"
                f"这将同时删除该提供商下的 **{model_count}** 个模型。\n"
                f"此操作不可撤销！",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 确认删除", callback_data=f"pm_delp_yes:{prov}")],
                    [InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")],
                ])
            )

        elif data.startswith("pm_delp_yes:"):
            prov = data[12:]
            pm = self._load_pm()
            pm.get("providers", {}).pop(prov, None)
            pm.get("models", {}).pop(prov, None)
            self._save_pm(pm)
            await query.edit_message_text(f"✅ 提供商 {prov} 及其所有模型已删除")

        elif data.startswith("pm_delm_p:"):
            prov = data[10:]
            pm = self._load_pm()
            models = pm.get("models", {}).get(prov, [])
            if not models:
                await query.edit_message_text("⚠️ 该提供商没有模型")
                return
            buttons = [[InlineKeyboardButton(f"🗑 {m}", callback_data=f"pm_delm:{prov}:{m}")] for m in models]
            buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
            await query.edit_message_text(f"🗑 删除 {prov} 的模型:", reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("pm_delm:"):
            parts = data.split(":", 2)
            prov, model = parts[1], parts[2]
            pm = self._load_pm()
            if prov in pm.get("models", {}):
                pm["models"][prov] = [m for m in pm["models"][prov] if m != model]
            self._save_pm(pm)
            try:
                await query.answer(f"🗑 已删除 {model}", show_alert=False)
            except Exception:
                pass
            # Refresh model list
            remaining = pm.get("models", {}).get(prov, [])
            if remaining:
                buttons = [[InlineKeyboardButton(f"🗑 {m}", callback_data=f"pm_delm:{prov}:{m}")] for m in remaining]
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="pm_cancel")])
                await query.edit_message_text(f"🗑 删除 {prov} 的模型 ({len(remaining)}):", reply_markup=InlineKeyboardMarkup(buttons))
            else:
                await query.edit_message_text(f"✅ {prov} 的模型已全部删除")

        # ── Edit agent model: 2-step provider → model selection ──
        elif data.startswith("em_prov:"):
            parts = data.split(":", 2)
            agent_name, prov = parts[1], parts[2]
            pm = self._load_pm()
            models = pm.get("models", {}).get(prov, [])
            if not models:
                self._edit_state[chat_id] = {"agent": agent_name, "field": "model", "provider": prov}
                await query.edit_message_text(
                    f"🏢 {prov} 暂无已注册模型\n\n"
                    "请直接输入模型ID:"
                )
                return
            buttons = [[InlineKeyboardButton(f"🤖 {m}", callback_data=f"em_model:{agent_name}:{prov}:{m}")] for m in models]
            buttons.append([InlineKeyboardButton("✏️ 手动输入", callback_data=f"em_manual:{agent_name}")])
            await query.edit_message_text(f"🏢 {prov} — 选择模型:", reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("em_model:"):
            parts = data.split(":", 3)
            agent_name, prov, model = parts[1], parts[2], parts[3]
            if self._groupchat_engine and agent_name in self._groupchat_engine.registry:
                self._groupchat_engine.registry[agent_name]["model"] = model
                # Update config on disk
                from pathlib import Path as _P
                agent_entry = self._groupchat_engine.registry[agent_name]
                if agent_entry.get("_default"):
                    # Default agent (Nanobot): update config.json
                    main_cfg_path = _P.home() / ".nanobot" / "config.json"
                    if main_cfg_path.exists():
                        cfg = json.loads(main_cfg_path.read_text())
                        cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = model
                        main_cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                else:
                    cfg_path = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "config.json"
                    if cfg_path.exists():
                        cfg = json.loads(cfg_path.read_text())
                        cfg["model"] = model
                        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                await query.edit_message_text(f"✅ {agent_name} 模型已更新:\n🏢 {prov} / 🤖 {model}")
            else:
                await query.edit_message_text(f"❌ Agent '{agent_name}' 不存在")
            self._edit_state.pop(chat_id, None)

        elif data.startswith("em_manual:"):
            agent_name = data[10:]
            self._edit_state[chat_id] = {"agent": agent_name, "field": "model"}
            await query.edit_message_text("请输入新模型名 (如 anthropic/claude-sonnet-4-5):")

        # ── Edit provider callbacks ──
        elif data.startswith("ep_pick:"):
            prov = data[8:]
            pm = self._load_pm()
            info = pm.get("providers", {}).get(prov, {})
            url = info.get("url", "?")
            key_preview = info.get("apiKey", "")[:8] + "..." if info.get("apiKey") else "(none)"
            buttons = [
                [InlineKeyboardButton("🔗 修改 URL", callback_data=f"ep_field:{prov}:url")],
                [InlineKeyboardButton("🔑 修改 API Key", callback_data=f"ep_field:{prov}:key")],
                [InlineKeyboardButton("📋 拉取模型列表", callback_data=f"ep_models:{prov}")],
                [InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")],
            ]
            await query.edit_message_text(
                f"✏️ 编辑提供商: {prov}\n\n"
                f"🔗 URL: {url}\n"
                f"🔑 Key: {key_preview}",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data.startswith("ep_field:"):
            parts = data.split(":", 2)
            prov, fld = parts[1], parts[2]
            self._edit_state[chat_id] = {"field": f"ep_{fld}", "mode": "pm", "prov_name": prov}
            prompts = {"url": "请输入新的 API Base URL:", "key": "请输入新的 API Key:"}
            await query.edit_message_text(f"✏️ {prov} — {prompts.get(fld, fld)}")

        elif data.startswith("ep_models:"):
            prov = data[10:]
            pm = self._load_pm()
            info = pm.get("providers", {}).get(prov, {})
            url = info.get("url", "").rstrip("/")
            api_key = info.get("apiKey", "")
            if not url or not api_key:
                await query.edit_message_text(f"⚠️ {prov} 缺少 URL 或 API Key")
                return
            # Fetch /v1/models (or /models)
            import aiohttp
            models_url = f"{url}/models" if "/v1" in url else f"{url}/v1/models"
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {api_key}"}
                    async with session.get(models_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            await query.edit_message_text(f"❌ 拉取失败 (HTTP {resp.status})\n{body[:200]}")
                            return
                        result = await resp.json()
            except Exception as e:
                await query.edit_message_text(f"❌ 拉取失败: {e}")
                return

            model_list = result.get("data", []) if isinstance(result, dict) else []
            if not model_list:
                await query.edit_message_text(f"⚠️ {prov} 无可用模型")
                return

            # Extract model IDs and sort
            model_ids = sorted(set(
                m.get("id", "") for m in model_list if m.get("id")
            ))

            # Already-added models
            existing = set(pm.get("models", {}).get(prov, []))

            # Cache for local rebuild after add
            if not hasattr(self, "_model_cache"):
                self._model_cache = {}
            self._model_cache[prov] = model_ids

            # Show first 30 models with add buttons
            lines = [f"📋 {prov} 可用模型 ({len(model_ids)}):\n"]
            buttons = []
            for mid in model_ids[:30]:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪ {mid}")
                    # Truncate callback_data to 64 bytes max
                    cb = f"ep_addm:{prov}:{mid}"
                    if len(cb.encode()) <= 64:
                        buttons.append([InlineKeyboardButton(f"+ {mid}", callback_data=cb)])
            if len(model_ids) > 30:
                lines.append(f"  ... 和 {len(model_ids) - 30} 个更多")
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")])

            text = "\n".join(lines)
            await query.edit_message_text(
                text[:4000],
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            )

        elif data.startswith("ep_addm:"):
            # ep_addm:provider:model_id — add model to provider
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            prov, model_id = parts[1], parts[2]
            pm = self._load_pm()
            if prov not in pm.get("providers", {}):
                await query.edit_message_text(f"❌ 提供商 {prov} 不存在")
                return
            models = pm.setdefault("models", {})
            prov_models = models.setdefault(prov, [])
            if model_id in prov_models:
                await query.edit_message_text(f"⚠️ {model_id} 已存在")
                return
            prov_models.append(model_id)
            self._save_pm(pm)
            # Reload in provider
            if self._groupchat_engine:
                self._groupchat_engine.provider._pm_overrides = None
            # Toast notification + refresh list locally
            try:
                await query.answer(f"✅ 已添加 {model_id}", show_alert=False)
            except Exception:
                pass
            # Rebuild model list from saved pm (no API re-fetch)
            existing = set(prov_models)
            all_models = getattr(self, "_model_cache", {}).get(prov, [])
            if not all_models:
                # No cache, just show confirmation
                await query.edit_message_text(
                    f"✅ 已添加 {model_id} 到 {prov}\n"
                    f"当前 {len(prov_models)} 个模型\n\n"
                    f"用 /editagent 切换 agent 模型",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 刷新列表", callback_data=f"ep_models:{prov}")],
                        [InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")],
                    ])
                )
                return
            # Rebuild from cache
            from nanobot.groupchat.engine import GroupChatEngine
            labels_map = {
                "web_search": "🔍", "web_fetch": "🌐", "exec": "⚡",
            }
            lines = [f"📋 {prov} 可用模型 ({len(all_models)}):\n"]
            buttons = []
            for mid in all_models[:30]:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪ {mid}")
                    cb = f"ep_addm:{prov}:{mid}"
                    if len(cb.encode()) <= 64:
                        buttons.append([InlineKeyboardButton(f"+ {mid}", callback_data=cb)])
            if len(all_models) > 30:
                lines.append(f"  ... 和 {len(all_models) - 30} 个更多")
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")])
            await query.edit_message_text(
                "\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            )

    async def _handle_edit_input(self, chat_id: str, content: str) -> None:
        """Process interactive edit state input."""
        state = self._edit_state[chat_id]
        field = state["field"]

        # Handle savegroup name input
        if field == "sg_name":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            result = self._groupchat_engine.save_group(content.strip())
            await self._gc_send(chat_id, result)
            return

        # Universal cancel — works at any edit prompt
        if content.strip() in ("0", "取消", "/cancel"):
            del self._edit_state[chat_id]
            await self._gc_send(chat_id, "❌ 已取消")
            return

        # Handle provider/model management flows
        if state.get("mode") == "pm":
            field = state["field"]
            if field == "pm_prov_name":
                name = content.strip().lower()
                state["prov_name"] = name
                state["field"] = "pm_prov_url"
                await self._gc_send(chat_id, f"提供商: {name}\n\n请输入 API Base URL\n(如 https://openrouter.ai/v1):")
                return
            elif field == "pm_prov_url":
                url = content.strip().rstrip("/")
                state["prov_url"] = url
                state["field"] = "pm_prov_key"
                await self._gc_send(chat_id, f"🔗 URL: {url}\n\n请输入 API Key:")
                return
            elif field == "pm_prov_key":
                api_key = content.strip()
                name = state["prov_name"]
                url = state["prov_url"]
                pm = self._load_pm()
                pm.setdefault("providers", {})[name] = {"url": url, "apiKey": api_key}
                pm.setdefault("models", {}).setdefault(name, [])
                self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ 提供商 {name} 已创建!\n🔗 {url}\n🔑 {api_key[:8]}...")
                return
            elif field == "pm_model_id":
                model_id = content.strip()
                prov = state["provider"]
                pm = self._load_pm()
                pm.setdefault("models", {}).setdefault(prov, [])
                if model_id not in pm["models"][prov]:
                    pm["models"][prov].append(model_id)
                self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ 模型已添加!\n🏢 {prov} / 🤖 {model_id}")
                return
            elif field == "ep_url":
                prov = state["prov_name"]
                pm = self._load_pm()
                if prov in pm.get("providers", {}):
                    pm["providers"][prov]["url"] = content.strip().rstrip("/")
                    self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ {prov} URL 已更新: {content.strip()}")
                return
            elif field == "ep_key":
                prov = state["prov_name"]
                pm = self._load_pm()
                if prov in pm.get("providers", {}):
                    pm["providers"][prov]["apiKey"] = content.strip()
                    self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ {prov} API Key 已更新: {content.strip()[:8]}...")
                return

        agent_name = state.get("agent", "")
        engine = self._groupchat_engine
        if not engine:
            del self._edit_state[chat_id]
            return



        # Handle /newagent create flow
        if state.get("mode") == "create":
            if field == "create_name":
                name = content.strip()
                if name in ("0", "取消"):
                    del self._edit_state[chat_id]
                    await self._gc_send(chat_id, "❌ 已取消")
                    return
                if engine._resolve_agent_name(name):
                    await self._gc_send(chat_id, f"⚠️ '{name}' 已存在，请换个名字:")
                    return
                state["agent"] = name
                state["field"] = "create_model"
                await self._gc_send(chat_id,
                    f"Agent: {name}\n\n请输入模型名:\n"
                    "(如 anthropic/claude-sonnet-4-5, x-ai/grok-4.1-fast)"
                )
                return
            if field == "create_model":
                model_name = content.strip()
                if model_name in ("0", "取消"):
                    del self._edit_state[chat_id]
                    await self._gc_send(chat_id, "❌ 已取消")
                    return
                await self._gc_send(chat_id, f"🔍 测试模型 {model_name}...")
                try:
                    response = await engine.provider.chat(
                        messages=[{"role": "user", "content": "Say 'hello' in one word."}],
                        model=model_name,
                        max_tokens=20,
                    )
                    reply = (response.content or "").strip()
                    state["model"] = model_name
                    state["field"] = "create_persona"
                    await self._gc_send(chat_id,
                        f"✅ 模型 {model_name} 可用!\n"
                        f"测试回复: {reply}\n\n"
                        f"请输入人设 (SOUL.md 内容):"
                    )
                except Exception as e:
                    await self._gc_send(chat_id,
                        f"❌ 模型 {model_name} 不可用: {e}\n\n"
                        f"请重新输入模型名，或发 0 取消:"
                    )
                return
            elif field == "create_persona":
                name = agent_name
                model = state["model"]
                prompt = content
                engine.registry[name] = {"model": model, "prompt": prompt}
                # Save to disk
                from pathlib import Path
                soul_dir = Path.home() / ".nanobot" / "agents" / name.lower() / "workspace"
                soul_dir.mkdir(parents=True, exist_ok=True)
                (soul_dir / "SOUL.md").write_text(prompt)
                config_path = soul_dir.parent / "config.json"
                import json
                config_path.write_text(json.dumps({"model": model}, indent=2))
                preview = prompt[:80] + "..." if len(prompt) > 80 else prompt
                await self._gc_send(chat_id, f"✅ Agent {name} 已创建!\n模型: {model}\n人设: {preview}")
                del self._edit_state[chat_id]
                return

        if field is None:
            c = content.strip()
            if c in ("0", "取消"):
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, "❌ 已取消")
                return
            field_map = {"1": "name", "2": "persona", "3": "model"}
            if c in field_map:
                state["field"] = field_map[c]
                prompts = {"name": "新名字", "persona": "新人设内容", "model": "新模型名 (如 anthropic/claude-sonnet-4-5)"}
                await self._gc_send(chat_id, f"请输入{prompts[field_map[c]]}:")
            else:
                await self._gc_send(chat_id, "请输入 1/2/3 或 0 取消")
            return

        if field == "name":
            new_name = content.strip()
            if new_name and new_name != agent_name:
                data = engine.registry.pop(agent_name)
                engine.registry[new_name] = data
                if agent_name in engine._active_agents:
                    idx = engine._active_agents.index(agent_name)
                    engine._active_agents[idx] = new_name
                # Rename directory
                from pathlib import Path
                agents_dir = Path.home() / ".nanobot" / "agents"
                old = agents_dir / agent_name.lower()
                new = agents_dir / new_name.lower()
                if old.exists() and not new.exists():
                    old.rename(new)
                engine._save_active()
                await self._gc_send(chat_id, f"✅ {agent_name} → {new_name}")
            else:
                await self._gc_send(chat_id, "⚠️ 名字未变")
        elif field == "persona":
            engine.registry[agent_name]["prompt"] = content
            from pathlib import Path
            soul_dir = Path.home() / ".nanobot" / "agents" / agent_name.lower() / "workspace"
            soul_dir.mkdir(parents=True, exist_ok=True)
            (soul_dir / "SOUL.md").write_text(content)
            preview = content[:80] + "..." if len(content) > 80 else content
            await self._gc_send(chat_id, f"✅ {agent_name} 人设已更新:\n{preview}")
        elif field == "model":
            new_model = content.strip()
            engine.registry[agent_name]["model"] = new_model
            # Persist to disk
            import json
            from pathlib import Path as _P
            agent_entry = engine.registry[agent_name]
            if agent_entry.get("_default"):
                main_cfg_path = _P.home() / ".nanobot" / "config.json"
                if main_cfg_path.exists():
                    cfg = json.loads(main_cfg_path.read_text())
                    cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = new_model
                    main_cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            else:
                cfg_path = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "config.json"
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text())
                    cfg["model"] = new_model
                    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            await self._gc_send(chat_id, f"✅ {agent_name} 模型: {new_model}")

        del self._edit_state[chat_id]

    async def _on_hyperparams(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """View or edit sampling parameters."""
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

        args = context.args or []
        if not args:
            lines = ["⚙️ 当前超参数:\n"]
            for k, v in params.items():
                lines.append(f"  {k}: {v}")
            lines.append(f"\n修改: /hyperparams <参数> <值>")
            lines.append(f"例如: /hyperparams temperature 0.8")
            await update.message.reply_text("\n".join(lines))
            return
        if len(args) < 2:
            await update.message.reply_text("用法: /hyperparams <参数> <值>")
            return
        key = args[0].lower()
        try:
            value = float(args[1])
        except ValueError:
            await update.message.reply_text("⚠️ 值必须是数字")
            return
        if key not in params:
            await update.message.reply_text(f"⚠️ 无效参数。可选: {', '.join(sorted(params.keys()))}")
            return
        old_val = params[key]
        params[key] = value
        # Persist to disk
        hp_path = Path.home() / ".nanobot" / "hyperparams.json"
        try:
            import json
            hp_path.write_text(json.dumps(params, indent=2))
        except Exception:
            pass
        await update.message.reply_text(f"✅ {key}: {old_val} → {value}\n即时生效，已持久化")

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
        """Hard reset: stop everything, clear all state."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        chat_id = str(update.message.chat_id)
        # Clear edit state
        self._edit_state.pop(chat_id, None)
        if self._groupchat_engine:
            # Force stop
            self._groupchat_engine._running = False
            if self._groupchat_engine._task and not self._groupchat_engine._task.done():
                self._groupchat_engine._task.cancel()
            self._groupchat_engine._task = None
            self._groupchat_engine._active_agents.clear()
            self._groupchat_engine._history.clear()
            self._groupchat_engine._request_log.clear()
            self._groupchat_engine._input_queue = __import__('asyncio').Queue()
            self._groupchat_engine._send_fn = None
            self._groupchat_engine._topic = ""
        await update.message.reply_text("🔄 系统已重置\n所有状态已清空，可以重新开始")

    # ── Group Config Commands ───────────────────────────────

    async def _on_savegroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        args = (context.args or [])
        if not args:
            if not self._groupchat_engine.active_agents:
                await update.message.reply_text("⚠️ 没有活跃 agent，无法保存")
                return
            members = ', '.join(self._groupchat_engine.active_agents)
            chat_id = str(update.message.chat_id)
            self._edit_state[chat_id] = {"field": "sg_name"}
            await update.message.reply_text(f"👥 当前成员: {members}\n\n请输入分组名称（或发送 0 取消）:")
            return
        name = " ".join(args)
        result = self._groupchat_engine.save_group(name)
        await update.message.reply_text(result)

    async def _on_loadgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        chat_id = str(update.message.chat_id)
        self._ensure_gc_send(chat_id)
        args = (context.args or [])
        if not args:
            groups = self._groupchat_engine._load_groups()
            if not groups:
                await update.message.reply_text("📋 没有保存的分组\n用 /savegroup 保存当前成员")
                return
            buttons = []
            for gname, members in groups.items():
                buttons.append([InlineKeyboardButton(
                    f"{gname} ({', '.join(members)})",
                    callback_data=f"lg:{gname}"
                )])
            await update.message.reply_text("📂 选择要载入的分组:", reply_markup=InlineKeyboardMarkup(buttons))
            return
        name = " ".join(args)
        result = self._groupchat_engine.load_group(name)
        await update.message.reply_text(result)

    async def _on_delgroup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        args = (context.args or [])
        if not args:
            groups = self._groupchat_engine._load_groups()
            if not groups:
                await update.message.reply_text("📋 没有保存的分组")
                return
            buttons = []
            for gname, members in groups.items():
                buttons.append([InlineKeyboardButton(
                    f"🗑 {gname} ({', '.join(members)})",
                    callback_data=f"dg:{gname}"
                )])
            await update.message.reply_text("🗑 选择要删除的分组:", reply_markup=InlineKeyboardMarkup(buttons))
            return
        name = " ".join(args)
        result = self._groupchat_engine.delete_group(name)
        await update.message.reply_text(result)

    async def _on_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return
        result = self._groupchat_engine.list_groups()
        await update.message.reply_text(result)

    async def _on_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Change agent speaking order."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("⚠️ 群聊引擎未初始化")
            return

        agents = self._groupchat_engine.active_agents
        if len(agents) < 2:
            await update.message.reply_text("⚠️ 至少需要 2 个活跃 agent 才能调整顺序")
            return

        args = context.args
        if args:
            # Direct: /order Ben Lucas Harper Grok
            result = self._groupchat_engine.reorder_agents(list(args))
            await update.message.reply_text(result)
            return

        # Interactive: show current order with move buttons
        await self._send_order_keyboard(str(update.message.chat_id), agents)

    async def _send_order_keyboard(self, chat_id: str, agents: list[str]) -> None:
        """Send inline keyboard to reorder agents."""
        order_str = " → ".join(agents)
        text = f"📢 当前发言顺序:\n{order_str}\n\n点击 ⬆ 上移:"
        buttons = []
        for i, name in enumerate(agents):
            if i > 0:  # Can't move first one up
                buttons.append([InlineKeyboardButton(f"⬆ {name}", callback_data=f"ord:{i}")])
        buttons.append([InlineKeyboardButton("✅ 完成", callback_data="ord:done")])
        await self._app.bot.send_message(
            chat_id=int(chat_id), text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _on_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show LLM call records (Langfuse-style) with pagination."""
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine:
            await update.message.reply_text("📭 无日志")
            return
        logs = self._groupchat_engine._request_log
        if not logs:
            await update.message.reply_text("📭 当前会话无 LLM 调用记录")
            return
        # Show last page
        page = max(0, (len(logs) - 1) // 10)
        text, markup = self._build_log_page(logs, page)
        await update.message.reply_text(text, reply_markup=markup)

    def _build_log_page(self, logs: list, page: int):
        """Build a log page (10 items per page)."""
        per_page = 10
        total = len(logs)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        start = page * per_page
        end = min(start + per_page, total)
        page_logs = logs[start:end]

        lines = [f"📊 LLM 调用记录 (第{page+1}/{total_pages}页, 共{total}条):\n"]
        buttons = []
        for i, r in enumerate(page_logs):
            idx = start + i
            agent = r.get("agent", "?")
            model = r.get("model", "?").split("/")[-1][:15]
            tokens = r.get("tokens", {})
            total_tok = tokens.get("total", 0)
            latency = r.get("latency", 0)
            iters = r.get("iterations", 1)
            tools = r.get("tools", [])
            error = r.get("error")
            status = "❌" if error else "✅"
            tool_str = f" 🔧×{len(tools)}" if tools else ""
            lines.append(
                f"{status} #{idx+1} {r.get('time','')} {agent} "
                f"[{model}] {total_tok}tok {latency}s"
                f" i{iters}{tool_str}"
            )
            buttons.append([InlineKeyboardButton(
                f"#{idx+1} {agent} — 详情",
                callback_data=f"logd:{idx}"
            )])

        # Pagination buttons
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"log_pg:{page-1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("下页 ➡️", callback_data=f"log_pg:{page+1}"))
        if nav:
            buttons.append(nav)

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "…"
        return text, InlineKeyboardMarkup(buttons) if buttons else None

    async def _on_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return
        if not self._groupchat_engine or not self._groupchat_engine.active_agents:
            await update.message.reply_text("没有活跃 agent")
            return
        self._ensure_gc_send(str(update.message.chat_id))
        self._groupchat_engine.request_summary()
        await update.message.reply_text("📋 正在生成总结...")

    @staticmethod
    def _sender_id(user) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    @staticmethod
    def _derive_topic_session_key(message) -> str | None:
        """Derive topic-scoped session key for non-private Telegram chats."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message.chat.type == "private" or message_thread_id is None:
            return None
        return f"telegram:{message.chat_id}:topic:{message_thread_id}"

    @staticmethod
    def _build_message_metadata(message, user) -> dict:
        """Build common Telegram inbound metadata payload."""
        return {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": message.chat.type != "private",
            "message_thread_id": getattr(message, "message_thread_id", None),
            "is_forum": bool(getattr(message.chat, "is_forum", False)),
        }

    async def _ensure_bot_identity(self) -> tuple[int | None, str | None]:
        """Load bot identity once and reuse it for mention/reply checks."""
        if self._bot_user_id is not None or self._bot_username is not None:
            return self._bot_user_id, self._bot_username
        if not self._app:
            return None, None
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        return self._bot_user_id, self._bot_username

    @staticmethod
    def _has_mention_entity(
        text: str,
        entities,
        bot_username: str,
        bot_id: int | None,
    ) -> bool:
        """Check Telegram mention entities against the bot username."""
        handle = f"@{bot_username}".lower()
        for entity in entities or []:
            entity_type = getattr(entity, "type", None)
            if entity_type == "text_mention":
                user = getattr(entity, "user", None)
                if user is not None and bot_id is not None and getattr(user, "id", None) == bot_id:
                    return True
                continue
            if entity_type != "mention":
                continue
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if offset is None or length is None:
                continue
            if text[offset : offset + length].lower() == handle:
                return True
        return handle in text.lower()

    async def _is_group_message_for_bot(self, message) -> bool:
        """Allow group messages when policy is open, @mentioned, or replying to the bot."""
        if message.chat.type == "private" or self.config.group_policy == "open":
            return True

        bot_id, bot_username = await self._ensure_bot_identity()
        if bot_username:
            text = message.text or ""
            caption = message.caption or ""
            if self._has_mention_entity(
                text,
                getattr(message, "entities", None),
                bot_username,
                bot_id,
            ):
                return True
            if self._has_mention_entity(
                caption,
                getattr(message, "caption_entities", None),
                bot_username,
                bot_id,
            ):
                return True

        reply_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
        return bool(bot_id and reply_user and reply_user.id == bot_id)

    def _remember_thread_context(self, message) -> None:
        """Cache topic thread id by chat/message id for follow-up replies."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return
        key = (str(message.chat_id), message.message_id)
        self._message_threads[key] = message_thread_id
        if len(self._message_threads) > 1000:
            self._message_threads.pop(next(iter(self._message_threads)))

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus for unified handling in AgentLoop."""
        if not update.message or not update.effective_user:
            return
        message = update.message
        user = update.effective_user
        self._remember_thread_context(message)

        # Clear interactive state on /new or /stop
        cmd = (message.text or "").strip().split()[0].lower() if message.text else ""
        if cmd in ("/new", "/stop"):
            str_chat_id = str(message.chat_id)
            self._edit_state.pop(str_chat_id, None)
            if cmd == "/new" and self._groupchat_engine:
                self._groupchat_engine._history.clear()
                self._groupchat_engine._request_log.clear()
                self._groupchat_engine._active_agents.clear()
                # Auto-add default agent so user can chat immediately
                if "Nanobot" in self._groupchat_engine.registry:
                    self._groupchat_engine.add_agent("Nanobot")

        await self._handle_message(
            sender_id=self._sender_id(user),
            chat_id=str(message.chat_id),
            content=message.text,
            metadata=self._build_message_metadata(message, user),
            session_key=self._derive_topic_session_key(message),
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = self._sender_id(user)
        self._remember_thread_context(message)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        if not await self._is_group_message_for_bot(message):
            return

        # Build content from text and/or media
        content_parts = []
        media_paths = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Handle media files
        media_file = None
        media_type = None

        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"

        # Download media if present
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(
                    media_type,
                    getattr(media_file, 'mime_type', None),
                    getattr(media_file, 'file_name', None),
                )
                media_dir = get_media_dir("telegram")

                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))

                media_paths.append(str(file_path))

                # Handle voice transcription
                if media_type == "voice" or media_type == "audio":
                    from nanobot.providers.transcription import GroqTranscriptionProvider
                    transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                    transcription = await transcriber.transcribe(file_path)
                    if transcription:
                        logger.info("Transcribed {}: {}...", media_type, transcription[:50])
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    content_parts.append(f"[{media_type}: {file_path}]")

                logger.debug("Downloaded {} to {}", media_type, file_path)
            except Exception as e:
                logger.error("Failed to download media: {}", e)
                content_parts.append(f"[{media_type}: download failed]")

        content = "\n".join(content_parts) if content_parts else "[empty message]"

        logger.debug("Telegram message from {}: {}...", sender_id, content[:50])

        str_chat_id = str(chat_id)
        metadata = self._build_message_metadata(message, user)
        session_key = self._derive_topic_session_key(message)

        # Telegram media groups: buffer briefly, forward as one aggregated turn.
        if media_group_id := getattr(message, "media_group_id", None):
            key = f"{str_chat_id}:{media_group_id}"
            if key not in self._media_group_buffers:
                self._media_group_buffers[key] = {
                    "sender_id": sender_id, "chat_id": str_chat_id,
                    "contents": [], "media": [],
                    "metadata": metadata,
                    "session_key": session_key,
                }
                self._start_typing(str_chat_id)
            buf = self._media_group_buffers[key]
            if content and content != "[empty message]":
                buf["contents"].append(content)
            buf["media"].extend(media_paths)
            if key not in self._media_group_tasks:
                self._media_group_tasks[key] = asyncio.create_task(self._flush_media_group(key))
            return

        # Start typing indicator before processing
        self._start_typing(str_chat_id)

        # Check for interactive edit state
        if str_chat_id in self._edit_state:
            await self._handle_edit_input(str_chat_id, content)
            self._stop_typing(str_chat_id)
            return

        # Route to active agents if any
        if self._groupchat_engine and self._groupchat_engine.active_agents:
            self._ensure_gc_send(str_chat_id)
            if self._groupchat_engine.is_running:
                # 2+ agents: inject message into group chat (async)
                # Keep typing indicator alive — agents are still generating
                self._groupchat_engine.inject(content)
            else:
                # 1 agent: direct chat (synchronous)
                response = await self._groupchat_engine.direct_chat(content)
                if response:
                    await self._gc_send(str_chat_id, response)
                self._stop_typing(str_chat_id)
            return

        # Engine exists but no active agents — don't fall through to main loop
        if self._groupchat_engine and not self._groupchat_engine.active_agents:
            await self._send_text(int(str_chat_id), "💤 没有活跃 agent，用 /addagent 加入一个")
            self._stop_typing(str_chat_id)
            return

        # Default: forward to the message bus (main agent loop)
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
            session_key=session_key,
        )

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            if not (buf := self._media_group_buffers.pop(key, None)):
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            await self._handle_message(
                sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                content=content, media=list(dict.fromkeys(buf["media"])),
                metadata=buf["metadata"],
                session_key=buf.get("session_key"),
            )
        finally:
            self._media_group_tasks.pop(key, None)

    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            while self._app:
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        logger.error("Telegram error: {}", context.error)

    def _get_extension(
        self,
        media_type: str,
        mime_type: str | None,
        filename: str | None = None,
    ) -> str:
        """Get file extension based on media type or original filename."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        if ext := type_map.get(media_type, ""):
            return ext

        if filename:
            from pathlib import Path

            return "".join(Path(filename).suffixes)

        return ""
