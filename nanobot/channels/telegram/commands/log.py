"""Log and summary commands for Telegram."""

from __future__ import annotations

import json
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from loguru import logger

from ..formatting import TELEGRAM_MAX_MESSAGE_LEN
from nanobot.utils.helpers import split_message


class LogCommandsMixin:
    """Mixin providing log and summary commands."""

    async def _on_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show persistent LLM request logs with session grouping and search.

        Usage: /log          — browse all logs (last page first)
               /log <keyword> — search logs by keyword
        """
        if not update.message or not update.effective_user:
            return
        if not self.is_allowed(self._sender_id(update.effective_user)):
            return

        keyword = " ".join(context.args).strip() if context.args else ""
        chat_id = str(update.message.chat_id)

        logs = self._load_request_logs()
        if not logs:
            await update.message.reply_text("📭 无 LLM 调用记录\n(记录保存在 ~/.nanobot/request_logs/)")
            return

        if keyword:
            # Store search keyword for callback pagination
            if not hasattr(self, "_log_search"):
                self._log_search: dict[str, str] = {}
            self._log_search[chat_id] = keyword
            logs = self._filter_logs(logs, keyword)
            if not logs:
                await update.message.reply_text(f"🔍 未找到匹配「{keyword}」的记录")
                return

        # Show LAST page first (newest entries)
        total_pages = max(1, (len(logs) + 7) // 8)
        page = total_pages - 1
        text, markup = self._build_log_page_v2(logs, page, keyword=keyword)
        await update.message.reply_text(text, reply_markup=markup)

    @staticmethod
    def _load_request_logs(max_lines: int = 500) -> list[dict]:
        """Load recent request logs from JSONL files."""
        import json as _json
        log_dir = Path.home() / ".nanobot" / "request_logs"
        if not log_dir.exists():
            return []
        files = sorted(log_dir.glob("*.jsonl"), reverse=True)
        logs: list[dict] = []
        for f in files:
            try:
                file_lines = f.read_text(encoding="utf-8").strip().split("\n")
                for line in reversed(file_lines):
                    if line.strip():
                        try:
                            logs.append(_json.loads(line))
                        except Exception:
                            pass
                    if len(logs) >= max_lines:
                        break
            except Exception:
                pass
            if len(logs) >= max_lines:
                break
        logs.reverse()
        return logs

    @staticmethod
    def _filter_logs(logs: list[dict], keyword: str) -> list[dict]:
        """Filter logs by keyword across agent, model, topic, reply_preview, error."""
        kw = keyword.lower()
        filtered = []
        for r in logs:
            searchable = " ".join([
                r.get("agent") or "",
                r.get("model") or "",
                r.get("topic") or "",
                r.get("session") or "",
                r.get("reply_preview") or "",
                r.get("error") or "",
                r.get("mode") or "",
            ]).lower()
            if kw in searchable:
                filtered.append(r)
        return filtered

    def _build_log_page_v2(self, logs: list[dict], page: int, keyword: str = ""):
        """Build a log page grouped by session, 8 items per page."""
        per_page = 8
        total = len(logs)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        start = page * per_page
        end = min(start + per_page, total)
        page_logs = logs[start:end]

        header = f"📊 LLM 请求日志 (第{page+1}/{total_pages}页, 共{total}条)"
        if keyword:
            header += f"\n🔍 关键词: {keyword}"
        lines = [header + "\n"]

        buttons = []
        last_session = None
        for i, r in enumerate(page_logs):
            idx = start + i
            # Session separator
            session = r.get("session") or "unknown"
            if session != last_session:
                topic = r.get("topic") or ""
                mode = r.get("mode") or ""
                mode_icon = "👤" if mode == "direct" else "👥"
                topic_str = f" — {topic}" if topic else ""
                lines.append(f"━━ {mode_icon} {session}{topic_str} ━━")
                last_session = session

            # Entry line
            agent = r.get("agent") or "?"
            model = (r.get("model") or "?").split("/")[-1][:18]
            status = "❌" if r.get("status") == "error" else "✅"
            latency = r.get("latency", 0)
            ts = (r.get("ts") or "")[-8:]  # HH:MM:SS
            usage = r.get("usage") or {}
            total_tok = usage.get("total", 0) or usage.get("total_tokens", 0)
            has_tc = " 🔧" if r.get("has_tool_calls") else ""
            stream_icon = "🔄" if r.get("stream") else ""
            cost = r.get("cost")
            cost_str = f" ${cost:.4f}" if cost else ""
            cache_t = r.get("cache_tokens")
            cache_str = f" 🔵" if cache_t else ""

            lines.append(
                f"{status} #{idx+1} {ts} {agent} [{model}] "
                f"{total_tok}tok{cost_str}{cache_str} {latency}s{has_tc}{stream_icon}"
            )
            # Button: agent + time for easy identification
            buttons.append([InlineKeyboardButton(
                f"#{idx+1} {ts} {agent} [{model}]",
                callback_data=f"rlog:{idx}"
            )])

        # Pagination
        nav = []
        if page > 0:
            cb_prefix = "rlogs_pg" if keyword else "rlog_pg"
            nav.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"{cb_prefix}:{page-1}"))
        if page < total_pages - 1:
            cb_prefix = "rlogs_pg" if keyword else "rlog_pg"
            nav.append(InlineKeyboardButton("下页 ➡️", callback_data=f"{cb_prefix}:{page+1}"))
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
                self._groupchat_engine.reset()
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

