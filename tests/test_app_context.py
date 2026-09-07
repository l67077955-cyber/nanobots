"""Tests for AppContext — the application-level context replacing global singletons.

See plan.md Phase 3.2.
"""

from pathlib import Path

from nanobot.state.app_context import AppContext, get_app_context, set_app_context


def test_app_context_create_with_defaults(tmp_path: Path):
    """AppContext.create() builds all default services."""
    ctx = AppContext.create(data_dir=tmp_path)
    assert ctx.data_dir == tmp_path
    assert ctx.event_bus is not None
    assert ctx.settings_store is not None
    assert ctx.mod_manager is not None


def test_app_context_create_with_custom_bus(tmp_path: Path):
    """AppContext.create() accepts a pre-configured event bus."""
    from nanobot.groupchat.orchestra.events import BroadcastEventDispatcher

    custom_bus = BroadcastEventDispatcher()
    ctx = AppContext.create(data_dir=tmp_path, event_bus=custom_bus)
    assert ctx.event_bus is custom_bus


def test_app_context_settings_store_wired(tmp_path: Path):
    """SettingsStore in the context uses the provided data_dir."""
    ctx = AppContext.create(data_dir=tmp_path)
    assert ctx.settings_store.data_dir == tmp_path


def test_app_context_singleton_get_set(tmp_path: Path):
    """get_app_context/set_app_context manage a module-level singleton."""
    # Reset
    set_app_context(None)
    ctx = AppContext.create(data_dir=tmp_path)
    set_app_context(ctx)
    assert get_app_context() is ctx
    # Reset back
    set_app_context(None)


def test_app_context_shutdown_handlers(tmp_path: Path):
    """Registered shutdown handlers are invoked on shutdown."""
    ctx = AppContext.create(data_dir=tmp_path)
    called = []
    ctx.register_shutdown_handler(lambda: called.append("sync"))
    import asyncio
    asyncio.run(ctx.shutdown())
    assert called == ["sync"]


def test_app_context_async_shutdown_handler(tmp_path: Path):
    """Async shutdown handlers are awaited."""
    ctx = AppContext.create(data_dir=tmp_path)
    called = []

    async def async_handler():
        called.append("async")

    ctx.register_shutdown_handler(async_handler)
    import asyncio
    asyncio.run(ctx.shutdown())
    assert called == ["async"]


def test_app_context_shutdown_swallows_errors(tmp_path: Path):
    """A failing shutdown handler doesn't break the others."""
    ctx = AppContext.create(data_dir=tmp_path)
    called = []

    def good():
        called.append("good")

    def bad():
        raise RuntimeError("boom")

    ctx.register_shutdown_handler(bad)
    ctx.register_shutdown_handler(good)
    import asyncio
    asyncio.run(ctx.shutdown())
    assert called == ["good"]


def test_app_context_event_helpers(tmp_path: Path):
    """emit_event / emit_event_async delegate to the bus."""
    from nanobot.groupchat.orchestra.events import BroadcastEventDispatcher

    bus = BroadcastEventDispatcher()
    received = []

    async def listener(**kwargs):
        received.append(kwargs)

    bus.on("test:event", listener)
    ctx = AppContext.create(data_dir=tmp_path, event_bus=bus)

    # Async emit
    import asyncio
    asyncio.run(ctx.emit_event_async("test:event", foo="bar"))
    assert received == [{"foo": "bar"}]
