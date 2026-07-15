"""Deprecated — use result_processor.py instead."""
from nanobot.groupchat.context.result_processor import (
    process_tool_result,
    maybe_persist_tool_result,
    STORAGE_DIR,
)

__all__ = ["process_tool_result", "maybe_persist_tool_result", "STORAGE_DIR"]
