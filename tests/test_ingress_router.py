"""IngressRouter regression tests for the one supported engine entry point."""

from __future__ import annotations

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.bus.router import IngressRouter


class _Engine:
    def __init__(self) -> None:
        self.injected: list[str] = []

    def inject(self, content: str) -> None:
        self.injected.append(content)


@pytest.mark.asyncio
async def test_router_delivers_inbound_content_through_engine_inject():
    """Regression: no duplicate deliver_user_message path may bypass inject."""
    engine = _Engine()
    router = IngressRouter(engine, bus=None)  # type: ignore[arg-type]
    msg = InboundMessage(
        channel="discord",
        sender_id="user-1",
        chat_id="room-1",
        content="hello group",
        media=["/tmp/image.png"],
        metadata={"source": "test"},
    )

    await router._deliver_to_engine(msg)

    assert engine.injected == ["hello group"]
