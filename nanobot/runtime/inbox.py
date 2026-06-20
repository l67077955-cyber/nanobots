"""File-drop inbox — drop a .txt/.md into ~/.nanobot/inbox/ to trigger the agent."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from nanobot.config.paths import get_inbox_dir, get_inbox_done_dir

POLL_INTERVAL_S = 2.0
_SUFFIXES = {".txt", ".md"}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def _process_file(engine, path: Path, done: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Inbox: cannot read {}: {}", path.name, exc)
        return

    dest = done / f"{_stamp()}_{path.name}"
    if not content:
        logger.info("Inbox: skip empty {}", path.name)
        path.rename(dest)
        return

    logger.info("Inbox: inject {} ({} chars)", path.name, len(content))
    engine.inject(f"[inbox:{path.name}]\n{content}")
    path.rename(dest)
    logger.info("Inbox: archived → {}", dest.name)


async def inbox_poller(engine, stop: asyncio.Event) -> None:
    inbox = get_inbox_dir()
    done = get_inbox_done_dir()
    processed: set[str] = set()

    logger.info("Inbox poller started ({})", inbox)

    while not stop.is_set():
        try:
            for path in sorted(inbox.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.suffix.lower() not in _SUFFIXES:
                    continue
                key = f"{path.name}:{path.stat().st_mtime_ns}"
                if key in processed:
                    continue
                processed.add(key)
                await _process_file(engine, path, done)
        except Exception as exc:
            logger.warning("Inbox poll error: {}", exc)

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


def start_inbox_poller(engine) -> tuple[asyncio.Task, asyncio.Event]:
    stop = asyncio.Event()
    return asyncio.create_task(inbox_poller(engine, stop)), stop