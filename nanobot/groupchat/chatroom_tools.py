"""Chatroom communication tools for inter-agent messaging.

Provides ``chatroom_send`` and ``wait`` tools that agents use
to communicate with each other during broadcast group chat rounds.
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.groupchat.mailbox import MailboxHub


class ChatroomSendTool(Tool):
    """Send a message to one or more agents in the group chat.

    Supports sending to specific agents by name, broadcasting
    to all agents with ``"All"``, or sending a summary to the
    user with ``"User"``.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "", send_fn=None) -> None:
        self._mailbox = mailbox
        self._agent_name = agent_name  # Set per-round by the engine
        self._send_fn = send_fn  # engine._send_fn for User target

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
            "REQUIRES two parameters: 'to' (target agent name, \"All\", or \"User\") and 'message' (the content). "
            "Use cases: (1) Share your findings with teammates, "
            "(2) Reply to a teammate's request with your results, "
            "(3) Ask a teammate for help or information, "
            "(4) Send final summary to the user with to=\"User\". "
            "IMPORTANT: When you receive a message from a teammate (via wait), "
            "you MUST reply back using chatroom_send — do not just include it in your final text response. "
            "Example: chatroom_send(to=\"Harper\", message=\"我搜到了3篇相关论文: ...\") "
            "Example: chatroom_send(to=\"User\", message=\"最终总结: ...\") "
            "Set 'to' to a specific agent name, a list of names, \"All\" to broadcast, or \"User\" to send to user."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "description": (
                        "Target agent name(s). Can be a single name (e.g. \"Harper\"), "
                        "a list (e.g. [\"Harper\", \"Lucas\"]), \"All\" to broadcast "
                        "to everyone, or \"User\" to send summary to user."
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

        # Handle "User" target — display directly to user
        user_sent = False
        agent_targets = []
        for t in targets:
            if t.lower() == "user":
                user_sent = True
            else:
                agent_targets.append(t)

        results = []
        if user_sent and self._send_fn:
            import asyncio
            try:
                await self._send_fn(
                    f"📋 **{self._agent_name} → 用户**:\n{message}"
                )
                results.append("✅ 已发送给用户")
            except Exception:
                results.append("⚠️ 发送给用户失败")
            # Also record in mailbox history
            self._mailbox.send(self._agent_name, ["User"], message)

        if agent_targets:
            delivered = self._mailbox.send(self._agent_name, agent_targets, message)
            target_str = ", ".join(agent_targets)
            results.append(f"✅ 消息已发送给 {target_str} ({delivered} 个 agent 收到)")

        return "  ".join(results) if results else "Error: no valid targets"


class WaitTool(Tool):
    """Wait for a message from another agent or an async task.

    Blocks the current agent's execution until a message arrives
    in its mailbox, or the timeout is reached.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "") -> None:
        self._mailbox = mailbox
        self._agent_name = agent_name

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

        msg = await self._mailbox.wait(
            agent_name=self._agent_name,
            timeout=float(min(timeout, 120)),
            from_agent=from_agent,
        )

        if msg is None:
            source = f"来自 {from_agent} 的" if from_agent else ""
            return f"⏰ 等待超时 ({timeout}s)，未收到{source}消息"

        return f"[{msg.sender}]: {msg.content}"
