"""Message handler for Telegram (text, photo, voice, document)."""

from __future__ import annotations

import asyncio
import json

from telegram import Update
from telegram.ext import ContextTypes

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.config.paths import get_media_dir
from .formatting import TELEGRAM_MAX_MESSAGE_LEN


class MessageHandlerMixin:
    """Mixin providing message handling."""

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

        downloaded_paths, downloaded_parts = await self._download_message_media(message)
        media_paths.extend(downloaded_paths)
        content_parts.extend(downloaded_parts)

        reply_ctx = self._extract_reply_context(message)
        if reply_ctx:
            content_parts.insert(0, reply_ctx)
        if getattr(message, "reply_to_message", None):
            reply_paths, reply_parts = await self._download_message_media(message.reply_to_message)
            if reply_paths:
                media_paths.extend(reply_paths)
            if not reply_ctx and reply_parts:
                content_parts.insert(0, f"[Reply to: {reply_parts[0]}]")

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

        if not self._groupchat_engine:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=str_chat_id,
                content=content,
                media=media_paths,
                metadata=metadata,
                session_key=session_key,
            )
            self._stop_typing(str_chat_id)
            return

        await self._dispatcher.handle(
            self,
            str_chat_id,
            sender_id,
            content,
            bus=self.bus,
            metadata=metadata,
            session_key_override=session_key,
            media=media_paths,
        )

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            if not (buf := self._media_group_buffers.pop(key, None)):
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            if self._groupchat_engine:
                await self.bus.publish_inbound(InboundMessage(
                    channel="telegram",
                    sender_id=buf["sender_id"],
                    chat_id=buf["chat_id"],
                    content=content,
                    media=list(dict.fromkeys(buf["media"])),
                    metadata=buf["metadata"],
                    session_key_override=buf.get("session_key"),
                ))
            else:
                await self._handle_message(
                    sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                    content=content, media=list(dict.fromkeys(buf["media"])),
                    metadata=buf["metadata"],
                    session_key=buf.get("session_key"),
                )
        finally:
            self._media_group_tasks.pop(key, None)

    @staticmethod
    def _extract_reply_context(message) -> str | None:
        reply = getattr(message, "reply_to_message", None)
        if not reply:
            return None
        text = getattr(reply, "text", None) or getattr(reply, "caption", None)
        if not text:
            return None
        return f"[Reply to: {text}]"

    async def _download_message_media(self, message) -> tuple[list[str], list[str]]:
        """Download Telegram media from a message and return paths + content markers."""
        media_file = None
        media_type = None
        if getattr(message, "photo", None):
            media_file = message.photo[-1]
            media_type = "image"
        elif getattr(message, "voice", None):
            media_file = message.voice
            media_type = "voice"
        elif getattr(message, "audio", None):
            media_file = message.audio
            media_type = "audio"
        elif getattr(message, "document", None):
            media_file = message.document
            media_type = "file"
        elif getattr(message, "video", None):
            media_file = message.video
            media_type = "file"
        elif getattr(message, "video_note", None):
            media_file = message.video_note
            media_type = "file"
        elif getattr(message, "animation", None):
            media_file = message.animation
            media_type = "file"

        if not media_file or not self._app or not getattr(self._app.bot, "get_file", None):
            return [], []

        try:
            file = await self._app.bot.get_file(media_file.file_id)
            ext = self._get_extension(
                media_type,
                getattr(media_file, "mime_type", None),
                getattr(media_file, "file_name", None),
            )
            import nanobot.channels.telegram as telegram_module

            media_dir = telegram_module.get_media_dir("telegram")
            stem = getattr(media_file, "file_unique_id", None) or getattr(media_file, "file_id", "file")
            file_path = media_dir / f"{stem[:64]}{ext}"
            await file.download_to_drive(str(file_path))
            path = str(file_path)
            return [path], [f"[{media_type}: {file_path}]"]
        except Exception as e:
            logger.error("Failed to download media: {}", e)
            return [], []

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
        err_str = str(context.error)
        logger.error("Telegram error: {}", err_str)
        # Show popup for stale button clicks
        if "Button_data_invalid" in err_str or "Query is too old" in err_str:
            if hasattr(update, "callback_query") and update.callback_query:
                try:
                    await update.callback_query.answer("⚠️ 该按钮已过期，请重新使用命令", show_alert=True)
                except Exception:
                    pass

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
