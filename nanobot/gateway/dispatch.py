"""Gateway inbound routing — slash commands and edit-state before MessageBus."""

from __future__ import annotations

from typing import Any, Protocol

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.channels.web_shim import WebContext, WebReplyMessage, WebUpdate, WebUser

# Single command table for every channel (Telegram, web, dashboard HTTP).
SLASH_COMMANDS: dict[str, str] = {
    "start": "_on_start",
    "new": "_forward_command",
    "clear": "_forward_command",
    "stop": "_forward_command",
    "cancel": "_on_cancel",
    "help": "_on_help",
    "agents": "_on_agents",
    "addagent": "_on_addagent",
    "removeagent": "_on_removeagent",
    "newagent": "_on_newagent",
    "editagent": "_on_editagent",
    "hyperparams": "_on_hyperparams",
    "restart": "_on_restart",
    "log": "_on_log",
    "savegroup": "_on_savegroup",
    "loadgroup": "_on_loadgroup",
    "delgroup": "_on_delgroup",
    "groups": "_on_groups",
    "order": "_on_order",
    "setleader": "_on_setleader",
    "prompt": "_on_prompt",
    "history": "_on_history",
    "newprovider": "_on_newprovider",
    "newmodel": "_on_newmodel",
    "deleteprovider": "_on_deleteprovider",
    "deletemodel": "_on_deletemodel",
    "editprovider": "_on_editprovider",
    "providers": "_on_providers",
    "speedtest": "_on_speedtest",
    "groupchat": "_on_groupchat",
    "summary": "_on_summary",
    "debug": "_on_debug",
}


class CommandHost(Protocol):
    """Channel surface required by InboundDispatcher."""

    name: str
    _edit_state: dict[str, dict]
    _groupchat_engine: Any

    def is_allowed(self, sender_id: str) -> bool: ...
    def _ensure_gc_send(self, chat_id: str) -> None: ...
    async def _handle_edit_input(self, chat_id: str, content: str) -> None: ...


def parse_slash_command(content: str) -> tuple[str, list[str]] | None:
    text = (content or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split()
    cmd_name = parts[0][1:].lower().split("@")[0]
    return cmd_name, parts[1:]


class InboundDispatcher:
    """Route slash commands and interactive edit input before bus inject."""

    async def handle(
        self,
        host: CommandHost,
        chat_id: str,
        sender_id: str,
        content: str,
        *,
        bus: Any = None,
        metadata: dict | None = None,
        session_key_override: str | None = None,
        media: list[str] | None = None,
        tg_update: Any = None,
        tg_context: Any = None,
    ) -> bool:
        """Return True if the message was consumed (do not publish to bus)."""
        if not host.is_allowed(sender_id):
            logger.warning("Inbound denied for {} on {}", sender_id, host.name)
            return True

        host._ensure_gc_send(chat_id)

        parsed = parse_slash_command(content)
        if parsed is not None:
            engine = getattr(host, "_groupchat_engine", None)
            if engine is not None and hasattr(engine, "interrupt_active_turn"):
                engine.interrupt_active_turn()
            cmd_name, args = parsed
            handler_name = SLASH_COMMANDS.get(cmd_name)
            if handler_name and hasattr(host, handler_name):
                if tg_update is not None:
                    # MessageHandler(filters.COMMAND) does not populate context.args;
                    # CommandHandler used to — restore that behavior here.
                    if tg_context is not None:
                        tg_context.args = list(args)
                    await getattr(host, handler_name)(tg_update, tg_context)
                else:
                    update = WebUpdate(
                        message=WebReplyMessage(chat_id, content, host),
                        effective_user=WebUser(id=sender_id),
                    )
                    await getattr(host, handler_name)(update, WebContext(args=args))
                return True

            if bus is not None:
                await bus.publish_outbound(OutboundMessage(
                    channel=host.name,
                    chat_id=chat_id,
                    content=f"❓ 未知命令: /{cmd_name}\n输入 /help 查看可用命令",
                    metadata=metadata or {},
                ))
                return True

        if chat_id in host._edit_state:
            await host._handle_edit_input(chat_id, content)
            return True

        if bus is None:
            return False

        if not host._groupchat_engine:
            return False

        await bus.publish_inbound(InboundMessage(
            channel=host.name,
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata=metadata or {},
            session_key_override=session_key_override,
        ))
        return True