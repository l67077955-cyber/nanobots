"""Gateway service wiring using the DI container.

Builds a ``Container`` with all services gateway() needs, so the wiring is
testable in isolation — tests can ``override()`` any provider before calling
``container.get(GroupChatEngine)``.

This is the testable complement to gateway()'s assembly code. gateway()
itself still constructs services inline for now (Phase 3.3 is optional); this
module is the migration target and a standalone-tested seam.

See plan.md Phase 3.3.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.state.container import Container

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import Config
    from nanobot.cron.service import CronService
    from nanobot.groupchat.orchestra.engine import GroupChatEngine
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import SessionManager


def build_gateway_container(
    config: "Config",
    workspace_path: Path,
) -> Container:
    """Build a DI container wired with gateway services.

    Registered singletons (lazily constructed):
    - Config            (the loaded runtime config)
    - MessageBus
    - LLMProvider       (via _make_provider)
    - SessionManager
    - CronService
    - GroupChatEngine

    Tests override any of these before resolving dependents:
        container.override(LLMProvider, fake_provider)
        engine = container.get(GroupChatEngine)
    """
    container = Container()

    # Config — instance, no factory needed.
    container.register_instance(_ConfigMarker, config)
    container.register_instance(_WorkspaceMarker, workspace_path)

    # MessageBus — simple singleton.
    from nanobot.bus.queue import MessageBus

    container.register_singleton(MessageBus, lambda _c: MessageBus())

    # LLMProvider — needs config.
    def _make_provider(_c: Container):
        from nanobot.cli.commands import _make_provider as _mp

        return _mp(_c.get(_ConfigMarker))

    from nanobot.providers.base import LLMProvider

    container.register_singleton(LLMProvider, _make_provider)

    # SessionManager.
    from nanobot.session.manager import SessionManager

    container.register_singleton(
        SessionManager,
        lambda _c: SessionManager(_c.get(_WorkspaceMarker)),
    )

    # CronService.
    from nanobot.config.paths import get_cron_dir
    from nanobot.cron.service import CronService

    container.register_singleton(
        CronService,
        lambda _c: CronService(get_cron_dir() / "jobs.json"),
    )

    # GroupChatEngine — the big one.
    from nanobot.groupchat.orchestra.engine import GroupChatEngine

    def _make_engine(c: Container):
        bus = c.get(MessageBus)
        provider = c.get(LLMProvider)
        cron = c.get(CronService)
        return GroupChatEngine(
            config=c.get(_ConfigMarker).groupchat,
            provider=provider,
            workspace=c.get(_WorkspaceMarker),
            cron_service=cron,
            send_outbound_fn=bus.publish_outbound,
        )

    container.register_singleton(GroupChatEngine, _make_engine)

    return container


# Marker types so we can register plain values (Config, Path) in the
# type-keyed container without colliding with real service types.
class _ConfigMarker:
    pass


class _WorkspaceMarker:
    pass
