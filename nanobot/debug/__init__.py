"""Debug library: direct primitives over current groupchat/provider runtime.

Aligned with runtime ports (CollabBus, AgentRunner, CycleController) and
groupchat_settings timeouts. Default dry-run; CLI supports --live.
"""

from nanobot.debug.primitives import (
    CallResult,
    Snapshot,
    call_llm,
    collab_interrupt,
    collab_send,
    collab_wait,
    decide_error_recovery,
    load_timeout_settings,
    snapshot_disk,
    snapshot_runtime,
    tool_loop_once,
)
from nanobot.debug.session import DebugSession

__all__ = [
    "CallResult",
    "DebugSession",
    "Snapshot",
    "call_llm",
    "collab_interrupt",
    "collab_send",
    "collab_wait",
    "decide_error_recovery",
    "load_timeout_settings",
    "snapshot_disk",
    "snapshot_runtime",
    "tool_loop_once",
]
