"""Async message queue for decoupled channel-agent communication."""

import asyncio

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage

# Default queue capacity — prevents unbounded memory growth if consumer stalls
DEFAULT_QUEUE_MAXSIZE = 1000

# Water mark thresholds for logging (fraction of maxsize)
_WATERMARK_HIGH = 0.8  # Log warning when queue exceeds 80%
_WATERMARK_CRITICAL = 0.95  # Log error when queue exceeds 95%


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.

    Queues have bounded capacity to provide backpressure — if the consumer
    cannot keep up, producers will block (await) or need to handle QueueFull.
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=maxsize)
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent.

        If the inbound queue is full, this will block until space is available,
        providing natural backpressure to slow producers.
        """
        await self.inbound.put(msg)
        self._check_watermark("inbound", self.inbound.qsize())

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        await self.outbound.put(msg)
        self._check_watermark("outbound", self.outbound.qsize())

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    def _check_watermark(self, queue_name: str, size: int) -> None:
        """Log warning/error if queue approaches capacity."""
        if self._maxsize <= 0:
            return  # Unbounded queue
        ratio = size / self._maxsize
        if ratio >= _WATERMARK_CRITICAL:
            logger.error(
                "MessageBus {} queue near capacity: {}/{} ({:.0f}%)",
                queue_name, size, self._maxsize, ratio * 100
            )
        elif ratio >= _WATERMARK_HIGH:
            logger.warning(
                "MessageBus {} queue above 80%: {}/{} ({:.0f}%)",
                queue_name, size, self._maxsize, ratio * 100
            )

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
