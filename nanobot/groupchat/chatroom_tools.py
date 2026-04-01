"""chatroom_tools.py — Agent 间通信工具（chatroom_send + wait）。

这两个工具会注册到每个 agent 的 tool_registry 中，
让 agent 能在 tool_loop 中调用它们来发送/接收消息。

工具：
    ChatroomSendTool — agent 调用 chatroom_send(to, message) 发送消息
    WaitTool         — agent 调用 wait(timeout, from_agent) 等待消息

底层：
    ChatroomSendTool → MailboxHub.send()
    WaitTool         → MailboxHub.wait()

⚠️ 不可修改工具的 name 和参数名 — agent prompt 中会引用它们。
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.groupchat.mailbox import MailboxHub



class ChatroomSendTool(Tool):
    """Send a message to one or more agents in the group chat.

    Supports sending to specific agents by name, broadcasting
    to all agents with ``"All"``.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "") -> None:
        self._mailbox = mailbox
        self._agent_name = agent_name

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

        # Reject "User" target
        targets = [t for t in targets if t.lower() != "user"]
        if not targets:
            return "⚠️ 不支持发送给 User。你的文字回复会自动展示给用户，直接写在回复里即可。"

        # Deduplicate: "All" already includes everyone
        if any(t.lower() == "all" for t in targets):
            targets = ["All"]

        delivered = self._mailbox.send(self._agent_name, targets, message)
        target_str = ", ".join(targets)
        return f"✅ sent to {target_str} ({delivered} delivered)"


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
