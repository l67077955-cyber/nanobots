"""Tests for the lightweight DI container. See plan.md Phase 3.3."""

import pytest

from nanobot.state.container import Container


class _Service:
    def __init__(self, value: int = 0) -> None:
        self.value = value


class _Dependent:
    def __init__(self, dep: _Service) -> None:
        self.dep = dep


def test_register_and_get_instance():
    c = Container()
    svc = _Service(value=42)
    c.register_instance(_Service, svc)
    assert c.get(_Service) is svc


def test_register_singleton_callable_factory():
    c = Container()
    c.register_singleton(_Service, lambda _c: _Service(value=7))
    first = c.get(_Service)
    second = c.get(_Service)
    assert first is second  # singleton: cached
    assert first.value == 7


def test_register_singleton_non_callable_instance():
    """register_singleton with a non-callable stores the instance directly."""
    c = Container()
    svc = _Service()
    c.register_singleton(_Service, svc)
    assert c.get(_Service) is svc


def test_register_factory_is_lazy_singleton():
    c = Container()
    c.register_factory(_Service, lambda _c: _Service())
    first = c.get(_Service)
    second = c.get(_Service)
    assert first is second  # cached as singleton


def test_register_transient_returns_new_instance_each_time():
    c = Container()
    c.register_transient(_Service, lambda _c: _Service())
    first = c.get(_Service)
    second = c.get(_Service)
    assert first is not second


def test_factory_resolves_dependencies():
    c = Container()
    c.register_singleton(_Service, lambda _c: _Service(value=1))
    c.register_factory(_Dependent, lambda _c: _Dependent(_c.get(_Service)))
    dep = c.get(_Dependent)
    assert dep.dep.value == 1


def test_override_takes_precedence():
    c = Container()
    c.register_singleton(_Service, lambda _c: _Service(value=1))
    stub = _Service(value=99)
    c.override(_Service, stub)
    assert c.get(_Service) is stub
    assert c.get(_Service).value == 99


def test_reset_overrides_clears_stub():
    c = Container()
    c.register_singleton(_Service, lambda _c: _Service(value=1))
    c.override(_Service, _Service(value=99))
    c.reset_overrides()
    assert c.get(_Service).value == 1


def test_reset_clears_instances_and_overrides():
    c = Container()
    c.register_singleton(_Service, lambda _c: _Service(value=1))
    first = c.get(_Service)
    c.reset()
    second = c.get(_Service)
    assert first is not second  # re-constructed


def test_get_unregistered_raises_keyerror():
    c = Container()
    with pytest.raises(KeyError):
        c.get(_Service)


def test_has_checks_registration():
    c = Container()
    assert not c.has(_Service)
    c.register_singleton(_Service, lambda _c: _Service())
    assert c.has(_Service)


def test_circular_dependency_detected():
    """A → B → A raises RuntimeError, not infinite recursion."""
    c = Container()

    class A:
        pass

    class B:
        def __init__(self, a: A) -> None:
            self.a = a

    c.register_factory(A, lambda _c: A())  # placeholder, overridden below
    c.register_factory(A, lambda _c: A())  # ensure A is in factories
    # Now wire a true cycle: A needs B, B needs A
    c.register_factory(A, lambda _c: A.__new__(A) if False else _c.get(B))  # noqa
    c.register_factory(B, lambda _c: B(_c.get(A)))
    with pytest.raises(RuntimeError, match="Circular dependency"):
        c.get(A)
