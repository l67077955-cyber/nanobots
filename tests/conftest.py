"""Shared pytest configuration / environment defaults.

The systemd gateway runs as a long-lived process holding the single-instance
flock on ~/.nanobot/gateway.lock. Gateway unit tests call ``gateway()`` purely
to exercise its setup path (config/workspace/port) and never start a bot, so a
live lock would false-block them. Skip the lock for the whole test suite; the
production lock behaviour is exercised at deploy time, not in unit tests.
"""
import os

os.environ.setdefault("NANOBOT_SKIP_LOCK", "1")