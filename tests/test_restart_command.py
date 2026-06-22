"""Tests for /restart slash command.

These tests cover the current implementation:
- /restart is a priority command (cmd_restart in nanobot.command.builtin).
- It always performs a *background* restart (perform_background_restart):
  * sets restart notice (env + for Telegram the /tmp json is written by caller)
  * returns "Restarting..."
  * spawns detached child (new session, logs redirected) and exits the current
    process. This closes any attached CLI/frontend terminal ("命令行界面")
    and continues the bot in the background.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nanobot.command.router import CommandContext


class TestRestartCommand:

    @pytest.mark.asyncio
    async def test_restart_sends_message_and_performs_background_restart(self):
        """Verify /restart uses background restart (no more in-place execv).

        This matches the requirement: when started from a command-line interface,
        /restart must close the frontend window/terminal and restart in the background.
        """
        from nanobot.command.builtin import cmd_restart
        from nanobot.utils.restart import (
            RESTART_NOTIFY_CHANNEL_ENV,
            RESTART_NOTIFY_CHAT_ID_ENV,
            RESTART_STARTED_AT_ENV,
        )

        # Minimal fake objects — cmd_restart only reads .msg from ctx
        fake_msg = SimpleNamespace(
            channel="cli",
            sender_id="user",
            chat_id="direct",
            content="/restart",
            metadata={},
        )
        fake_loop = MagicMock()
        ctx = CommandContext(msg=fake_msg, session=None, key="cli:direct", raw="/restart", loop=fake_loop)

        async def _fast_sleep(_delay: float) -> None:
            return None

        scheduled: list[asyncio.Task] = []

        def _capture_task(coro):
            task = asyncio.create_task(coro)
            scheduled.append(task)
            return task

        fake_asyncio = SimpleNamespace(
            sleep=_fast_sleep,
            create_task=_capture_task,
        )

        with patch.dict(os.environ, {}, clear=False), \
             patch("nanobot.command.builtin.asyncio", new=fake_asyncio), \
             patch("nanobot.utils.restart.perform_background_restart") as mock_bg_restart:
            # The inner "from nanobot.utils.restart import perform_background_restart"
            # inside cmd_restart's _do_restart will pick up this patch.
            out = await cmd_restart(ctx)
            assert "Restarting" in out.content
            assert os.environ.get(RESTART_NOTIFY_CHANNEL_ENV) == "cli"
            assert os.environ.get(RESTART_NOTIFY_CHAT_ID_ENV) == "direct"
            assert os.environ.get(RESTART_STARTED_AT_ENV)

            assert scheduled
            # The scheduled task calls perform_background_restart (after the tiny delay inside it)
            await scheduled[0]
            mock_bg_restart.assert_called_once()
