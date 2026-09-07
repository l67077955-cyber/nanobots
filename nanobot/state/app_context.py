"""Application-level context to replace global singletons.

Provides a unified container for application-wide services, supporting:
- Dependency injection for testing
- Multiple session scenarios
- Clean lifecycle management

Usage:
    ctx = AppContext.create()
    engine = GroupChatEngine(context=ctx)
    # Or with custom data_dir:
    ctx = AppContext.create(data_dir=Path("/custom/data"))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.groupchat.orchestra.events import BroadcastEventDispatcher
    from nanobot.mods.manager import ModManager
    from nanobot.state.settings_store import SettingsStore


@dataclass
class AppContext:
    """Application-level context, replacing global singletons.

    Holds all application-wide services and configuration, making
    dependency injection straightforward and supporting testing.

    Attributes:
        event_bus: Event dispatcher for mods and internal events.
        settings_store: Unified settings persistence service.
        mod_manager: Mod plugin lifecycle manager.
        data_dir: Root data directory (~/.nanobot by default).
    """

    event_bus: "BroadcastEventDispatcher"
    settings_store: "SettingsStore"
    mod_manager: "ModManager"
    data_dir: Path

    # Optional runtime state
    _shutdown_handlers: list = field(default_factory=list)

    @classmethod
    def create(
        cls,
        data_dir: Path | None = None,
        event_bus: "BroadcastEventDispatcher | None" = None,
    ) -> "AppContext":
        """Create an application context with default services.

        Args:
            data_dir: Optional override for ~/.nanobot directory.
            event_bus: Optional pre-configured event bus (for testing).

        Returns:
            Configured AppContext instance.
        """
        from nanobot.groupchat.orchestra.events import BroadcastEventDispatcher
        from nanobot.mods.manager import ModManager
        from nanobot.state.settings_store import SettingsStore

        data_dir = data_dir or Path.home() / ".nanobot"

        # Use provided bus or create default
        bus = event_bus or BroadcastEventDispatcher()

        # Create services
        settings_store = SettingsStore(data_dir)
        mod_manager = ModManager(bus, send=None)  # send will be set later

        return cls(
            event_bus=bus,
            settings_store=settings_store,
            mod_manager=mod_manager,
            data_dir=data_dir,
        )

    def register_shutdown_handler(self, handler) -> None:
        """Register a cleanup handler to be called on shutdown."""
        self._shutdown_handlers.append(handler)

    async def shutdown(self) -> None:
        """Invoke all registered shutdown handlers."""
        for handler in self._shutdown_handlers:
            try:
                result = handler()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass  # Log but don't fail shutdown
        self._shutdown_handlers.clear()

    # Convenience accessors for common operations

    def get_provider(self, name: str) -> dict | None:
        """Get a provider configuration."""
        return self.settings_store.get_provider(name)

    def get_agent_config(self, name: str) -> dict:
        """Get an agent configuration."""
        return self.settings_store.load_agent(name)

    def emit_event(self, event_name: str, **kwargs) -> None:
        """Emit an event (sync, fire-and-forget)."""
        self.event_bus.emit_nowait(event_name, **kwargs)

    async def emit_event_async(self, event_name: str, **kwargs) -> None:
        """Emit an event (async, sequential)."""
        await self.event_bus.emit(event_name, **kwargs)


# Module-level singleton for backward compatibility
_context: AppContext | None = None


def get_app_context() -> AppContext:
    """Get the global AppContext singleton.

    Creates the singleton on first access. Use AppContext.create()
    for dependency injection in tests.
    """
    global _context
    if _context is None:
        _context = AppContext.create()
    return _context


def set_app_context(ctx: AppContext | None) -> None:
    """Set or reset the global context (for testing)."""
    global _context
    _context = ctx
