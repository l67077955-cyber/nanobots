"""Chatroom communication tools for inter-agent messaging.

Core tools:
- ``ChatroomSendTool``: Send messages to other agents
- ``WaitTool``: Wait for messages from other agents
- ``YieldTurnTool``: Yield speaking turn to a teammate

Search and leader tools have been extracted to:
- ``search_tools.py``: SearchPool, CachedSearchTool, SmartSearchTool, SmartFetchTool
- ``leader_tools.py``: LeaderGate, ManageAgentTool, EndDiscussionTool, TransferCreditsTool
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool, SpeakQueue

# ── Lazy re-exports for backward compatibility ─────────────────
# Avoids circular import: search_tools → agent.tools.base → agent.__init__
# → agent.loop → chatroom_tools → search_tools

_SEARCH_NAMES = {"SearchPool", "CachedSearchTool", "SmartSearchTool", "SmartFetchTool"}
_LEADER_NAMES = {"LeaderGate", "ManageAgentTool", "EndDiscussionTool", "TransferCreditsTool"}


def __getattr__(name: str):
    if name in _SEARCH_NAMES:
        from nanobot.groupchat import search_tools
        return getattr(search_tools, name)
    if name in _LEADER_NAMES:
        from nanobot.groupchat import leader_tools
        return getattr(leader_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ChatroomSendTool(Tool):
    """Send a message to one or more agents in the group chat.

    Supports sending to specific agents by name, broadcasting
    to all agents with ``"All"``, or sending a summary to the
    user with ``"User"``.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "", pool: ConversationPool | None = None,
                 search_pool: "SearchPool | None" = None,
                 leader_gate: "LeaderGate | None" = None) -> None:
        self._mailbox = mailbox
        self._agent_name = agent_name  # Set per-round by the engine
        self._pool = pool
        self._search_pool = search_pool  # for credit recovery on successful sends
        self._last_received_from: str | None = None  # track who we last received from
        self._leader_gate = leader_gate

    def set_agent(self, name: str) -> None:
        """Set which agent is using this tool instance."""
        self._agent_name = name

    @property
    def name(self) -> str:
        return "chatroom_send"

    @property
    def description(self) -> str:
        return (
            "Send a message to other agents in the group chat. "
            "REQUIRES two parameters: 'to' (target agent name or \"All\") and 'message' (the content). "
            "Use cases: (1) Share your findings with teammates, "
            "(2) Reply to a teammate's request with your results, "
            "(3) Ask a teammate for help or information. "
            "IMPORTANT: When you receive a message from a teammate (via wait), "
            "you MUST reply back using chatroom_send — do not just include it in your final text response. "
            "Example: chatroom_send(to=\"Harper\", message=\"我搜到了3篇相关论文: ...\") "
            "Example: chatroom_send(to=\"All\", message=\"我的发现: ...\") "
            "Set 'to' to a specific agent name, a list of names, or \"All\" to broadcast. "
            "注意：不要发送给 User，你的文字回复会自动展示给用户。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "description": (
                        "Target agent name(s). Can be a single name (e.g. \"Harper\"), "
                        "a list (e.g. [\"Harper\", \"Lucas\"]), or \"All\" to broadcast "
                        "to everyone. Do NOT send to \"User\"."
                    ),
                    # Accept both string and array via oneOf
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "message": {
                    "type": "string",
                    "description": "The message content to send to the target agent(s).",
                },
            },
            "required": ["to", "message"],
        }

    async def execute(self, to: str | list[str] = "All", message: str = "", **kwargs: Any) -> str:
        if not self._agent_name:
            return "Error: agent context not set"
        if not message:
            return "Error: message cannot be empty"

        # Normalize targets
        if isinstance(to, str):
            targets = [to]
        elif isinstance(to, list):
            targets = to
        else:
            targets = [str(to)]

        # Reject "User" target — agent text response is auto-displayed
        targets = [t for t in targets if t.lower() != "user"]
        if not targets:
            return "⚠️ 不支持发送给 User。你的文字回复会自动展示给用户，直接写在回复里即可。"

        # ── Leader gate: non-leader agents limited to 1 message between leader messages ──
        if self._leader_gate and not self._leader_gate.try_send(self._agent_name):
            leader = self._leader_gate.leader
            return (
                f"⚠️ 你已发过 1 条消息，必须等待 Leader ({leader}) 发言后才能再发。"
                f"请用 wait() 等待 {leader} 的回复。"
            )

        # Deduplicate: "All" already includes everyone, strip individual names
        if any(t.lower() == "all" for t in targets):
            targets = ["All"]

        # Expand "All" to actual agent names for slot counting
        if "All" in targets:
            actual_recipients = [a for a in self._mailbox.agent_names if a != self._agent_name]
        else:
            actual_recipients = [t for t in targets if t != self._agent_name]

        # Allocate conversation slots (blocks if pool exhausted)
        if self._pool:
            ok = await self._pool.allocate(self._agent_name, actual_recipients)
            if not ok:
                return (
                    f"BLOCKED: pool full ({self._pool.used}/{self._pool.capacity}), "
                    "message dropped. Use wait() to free slots, or send to fewer people."
                )
            # If replying to someone who sent us a message, mark it replied
            if self._last_received_from:
                self._pool.mark_replied(self._agent_name, self._last_received_from)

        delivered = self._mailbox.send(self._agent_name, targets, message)

        # Record send in leader gate
        if self._leader_gate:
            self._leader_gate.record_send(self._agent_name)

        # Count successful sends as "output" for search credit recovery
        if delivered > 0 and self._search_pool:
            self._search_pool.on_output(self._agent_name)

        avail_hint = ""
        if self._pool:
            avail_hint = f" [{self._pool.used}/{self._pool.capacity} threads]"
        search_hint = ""
        if self._search_pool:
            c = self._search_pool.agent_credits(self._agent_name)
            search_hint = f" [🔍{c}]"
        target_str = ", ".join(targets)
        return f"✅ sent to {target_str} ({delivered} delivered){avail_hint}{search_hint}"


class WaitTool(Tool):
    """Wait for a message from another agent or an async task.

    Blocks the current agent's execution until a message arrives
    in its mailbox, or the timeout is reached.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "", pool: ConversationPool | None = None) -> None:
        self._mailbox = mailbox
        self._agent_name = agent_name
        self._pool = pool
        self._send_tool: ChatroomSendTool | None = None  # linked for last_received tracking

    def set_agent(self, name: str) -> None:
        """Set which agent is using this tool instance."""
        self._agent_name = name

    @property
    def name(self) -> str:
        return "wait"

    @property
    def description(self) -> str:
        return (
            "Wait for a message from another agent. "
            "Use this after sending a message with chatroom_send to wait for a reply. "
            "Has a hard timeout of 120 seconds per call. "
            "Returns the message content, or a timeout notice."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to wait (default: 30, hard limit: 120).",
                    "minimum": 1,
                    "maximum": 120,
                },
                "from_agent": {
                    "type": "string",
                    "description": (
                        "Optional: only wait for a message from this specific agent. "
                        "Leave empty to accept messages from anyone."
                    ),
                },
            },
            "required": [],
        }

    async def execute(
        self,
        timeout: int = 30,
        from_agent: str = "",
        **kwargs: Any,
    ) -> str:
        if not self._agent_name:
            return "Error: agent context not set"

        # Release unread slots before waiting ("not replying" to pending messages)
        released = 0
        if self._pool:
            released = self._pool.release_unread(self._agent_name)

        msg = await self._mailbox.wait(
            agent_name=self._agent_name,
            timeout=float(min(timeout, 120)),
            from_agent=from_agent,
        )

        if msg is None:
            source = f"来自 {from_agent} 的" if from_agent else ""
            return f"⏰ 等待超时 ({timeout}s)，未收到{source}消息"

        # Track who we received from → next chatroom_send knows it's a "reply"
        if self._send_tool:
            self._send_tool._last_received_from = msg.sender

        return f"[{msg.sender}]: {msg.content}"


class YieldTurnTool(Tool):
    """Yield the current speaking turn to a specific teammate.

    When an agent decides another teammate is better suited to speak
    next, it can yield its turn. The yielding agent's timestamp is
    refreshed (counts as having spoken), so it moves to the back of
    the LRU queue.
    """

    def __init__(self, speak_queue: SpeakQueue, agent_name: str = "") -> None:
        self._speak_queue = speak_queue
        self._agent_name = agent_name

    def set_agent(self, name: str) -> None:
        self._agent_name = name

    @property
    def name(self) -> str:
        return "yield_turn"

    @property
    def description(self) -> str:
        return (
            "让出你的发言机会给指定队友。"
            "使用场景：你觉得某个队友更适合先发言，或你暂时没有新观点。"
            "让出后，你的发言顺序会被刷新（视为已发言），排到队列后面。"
            "Example: yield_turn(to=\"Harper\", reason=\"她对这个话题更专业\")"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "要让出发言机会的队友名字",
                },
                "reason": {
                    "type": "string",
                    "description": "让出的原因（可选）",
                },
            },
            "required": ["to"],
        }

    async def execute(self, to: str = "", reason: str = "", **kwargs: Any) -> str:
        if not self._agent_name:
            return "Error: agent context not set"
        if not to:
            return "Error: 必须指定让出给谁 (to)"

        ok = await self._speak_queue.yield_to(self._agent_name, to)
        if not ok:
            return f"⚠️ 队友 '{to}' 不存在"

        reason_str = f"（原因: {reason}）" if reason else ""
        return f"✅ 已将发言机会让给 {to}{reason_str}"
