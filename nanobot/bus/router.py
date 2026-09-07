"""Ingress router for MessageBus message routing.

Consumes messages from the MessageBus inbound queue and routes them to
the appropriate handlers (GroupChatEngine or command handlers).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.bus.events import InboundMessage

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.groupchat.orchestra.engine import GroupChatEngine


class IngressRouter:
    """Consumes MessageBus inbound queue and routes to correct handlers.

    This fixes the core architectural issue where MessageBus.consume_inbound()
    was never called - channels published to a dead queue. Now all channels
    can use the unified publish_inbound() path.
    """

    def __init__(
        self,
        engine: GroupChatEngine,
        bus: MessageBus,
        command_handlers: dict[str, Callable] | None = None,
    ):
        """Initialize the router.

        Args:
            engine: The GroupChatEngine for message delivery.
            bus: The MessageBus to consume from.
            command_handlers: Optional channel-specific command handlers.
        """
        self._engine = engine
        self._bus = bus
        self._command_handlers = command_handlers or {}
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the consumer loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info("IngressRouter: started consuming MessageBus inbound queue")

    async def stop(self) -> None:
        """Stop the consumer loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("IngressRouter: stopped")

    async def _consume_loop(self) -> None:
        """Main consumer loop - blocks on consume_inbound()."""
        while self._running:
            try:
                msg = await self._bus.consume_inbound()
                await self._route(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("IngressRouter error: {}", e)
                await asyncio.sleep(1)  # Back off on error

    async def _route(self, msg: InboundMessage) -> None:
        """Route an inbound message to the correct handler.

        Args:
            msg: The inbound message from the bus.
        """
        content = msg.content.strip()

        # Command routing: messages starting with /
        if content.startswith("/"):
            handler = self._command_handlers.get(msg.channel)
            if handler:
                try:
                    await handler(msg)
                    return
                except Exception as e:
                    logger.error("Command handler error for {}: {}", msg.channel, e)

        # Default: deliver to GroupChatEngine
        await self._deliver_to_engine(msg)

    async def _deliver_to_engine(self, msg: InboundMessage) -> None:
        """Deliver a message to the GroupChatEngine.

        Args:
            msg: The inbound message to deliver.
        """
        # inject() is the single GroupChatEngine ingress decision point.
        # Inbound media/metadata remain owned by the channel bus; group-chat
        # history currently persists textual turns only.
        self._engine.inject(msg.content)

    def register_command_handler(self, channel: str, handler: Callable) -> None:
        """Register a command handler for a specific channel.

        Args:
            channel: The channel name.
            handler: Async callable that takes InboundMessage.
        """
        self._command_handlers[channel] = handler
