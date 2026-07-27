"""Deprecated — use result_processor.py instead."""
from nanobot.groupchat.history.result_processor import (
    STORAGE_DIR,
    maybe_persist_tool_result,
    process_tool_result,
)

__all__ = ["process_tool_result", "maybe_persist_tool_result", "STORAGE_DIR"]
