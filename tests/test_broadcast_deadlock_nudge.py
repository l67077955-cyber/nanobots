"""Regression: all-waiting deadlock nudge must not NameError on random."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.groupchat.runtime import broadcast as broadcast_mod


def test_broadcast_module_imports_random():
    """Deadlock path uses random.choice; module must bind the name."""
    assert hasattr(broadcast_mod, "random")
    assert callable(broadcast_mod.random.choice)


def test_random_choice_usable_for_deadlock_nudge():
    """Mirror the deadlock snippet from _run_one."""
    _active = ["Harper", "Kirk"]
    # must not raise NameError
    target = broadcast_mod.random.choice(_active)
    assert target in _active
