"""Lightweight dependency injection container.

A minimal DI container that makes gateway() wiring testable without pulling
in a heavy external framework. Supports singleton and factory providers, and
override for tests.

Usage:
    container = Container()
    container.register_singleton(MessageBus, MessageBus())
    container.register_factory(Provider, lambda c: _make_provider(c.get(Config)))

    bus = container.get(MessageBus)

    # Test override:
    container.override(MessageBus, fake_bus)

See plan.md Phase 3.3.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

T = TypeVar("T")


class Container:
    """Minimal DI container with singleton/factory providers and test overrides.

    Design goals:
    - No external dependencies.
    - Lazy construction via factories (resolves dependencies on first get()).
    - Test-friendly: ``override()`` swaps any provider for a fixed instance.
    - Cycle detection via a "currently constructing" set.
    """

    def __init__(self) -> None:
        # Mapping[Type, factory] for lazy construction.
        self._factories: dict[type, Callable[["Container"], Any]] = {}
        # Singleton instances already constructed.
        self._instances: dict[type, Any] = {}
        # Direct overrides (test stubs) — take precedence over factories.
        self._overrides: dict[type, Any] = {}
        # Types currently being constructed (cycle detection).
        self._constructing: set[type] = set()

    # ── Registration ────────────────────────────────────────────────────────

    def register_singleton(self, iface: type[T], factory: Callable[["Container"], T] | T) -> None:
        """Register a singleton. If *factory* is callable, it is invoked once
        with the container on first ``get()``; otherwise the instance is
        stored directly.
        """
        if callable(factory) and not isinstance(factory, type):
            self._factories[iface] = factory
        else:
            self._instances[iface] = factory
        self._overrides.pop(iface, None)

    def register_factory(self, iface: type[T], factory: Callable[["Container"], T]) -> None:
        """Register a factory provider. The factory is called every time
        ``get()`` is invoked (transient)."""
        # Factories are stored as-is; we distinguish transient vs singleton by
        # presence in _instances after first construction. For simplicity we
        # treat register_factory as a lazy singleton here (most gateway wiring
        # wants singletons). Use register_transient for true transients.
        self._factories[iface] = factory
        self._instances.pop(iface, None)
        self._overrides.pop(iface, None)

    def register_transient(self, iface: type[T], factory: Callable[["Container"], T]) -> None:
        """Register a transient provider — a fresh instance per ``get()``.

        Marked transients are stored with a sentinel so ``get()`` always
        re-invokes the factory instead of caching.
        """
        self._factories[iface] = _Transient(factory)

    def register_instance(self, iface: type[T], instance: T) -> None:
        """Register a pre-built instance directly."""
        self._instances[iface] = instance
        self._factories.pop(iface, None)
        self._overrides.pop(iface, None)

    # ── Resolution ─────────────────────────────────────────────────────────

    def get(self, iface: type[T]) -> T:
        """Resolve *iface* to an instance.

        Resolution order: override → cached instance → factory.
        """
        if iface in self._overrides:
            return self._overrides[iface]  # type: ignore[no-any-return]

        if iface in self._instances:
            return self._instances[iface]  # type: ignore[no-any-return]

        if iface in self._factories:
            return self._construct(iface)  # type: ignore[no-any-return]

        raise KeyError(f"No provider registered for {iface!r}")

    def _construct(self, iface: type[T]) -> T:
        factory = self._factories[iface]

        # Transient: always re-invoke, no caching.
        if isinstance(factory, _Transient):
            return factory(self)  # type: ignore[no-any-return]

        # Singleton: invoke once, cache, detect cycles.
        if iface in self._constructing:
            raise RuntimeError(f"Circular dependency detected while constructing {iface!r}")
        self._constructing.add(iface)
        try:
            instance = factory(self)
        finally:
            self._constructing.discard(iface)
        self._instances[iface] = instance
        return instance  # type: ignore[no-any-return]

    # ── Test support ────────────────────────────────────────────────────────

    def override(self, iface: type[T], instance: T) -> None:
        """Override a provider with a fixed instance (test stubs)."""
        self._overrides[iface] = instance

    def reset_overrides(self) -> None:
        """Clear all overrides."""
        self._overrides.clear()

    def reset(self) -> None:
        """Clear all instances and overrides (force re-construction)."""
        self._instances.clear()
        self._overrides.clear()
        self._constructing.clear()

    def has(self, iface: type) -> bool:
        """Check if a provider is registered."""
        return iface in self._factories or iface in self._instances or iface in self._overrides


class _Transient:
    """Marker wrapper distinguishing transient factories from singleton ones."""

    def __init__(self, factory: Callable[["Container"], Any]) -> None:
        self._factory = factory

    def __call__(self, container: Container) -> Any:
        return self._factory(container)
