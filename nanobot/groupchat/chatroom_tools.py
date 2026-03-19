"""Chatroom communication tools for inter-agent messaging.

Provides ``chatroom_send`` and ``wait`` tools that agents use
to communicate with each other during broadcast group chat rounds.
Also contains ``CachedSearchTool`` for cross-agent search deduplication.
"""

from __future__ import annotations

import re
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool, SpeakQueue


class SearchBudget:
    """Per-agent search credit system.

    - Each agent starts with `initial` credits
    - Each web_search/web_fetch costs 1 credit
    - Each chatroom_send earns +1 credit (up to `max_budget`)
    - Credits are NOT returned after search completes

    This incentivizes agents to participate in conversation
    before doing more searches.
    """

    def __init__(self, agents: list[str], initial: int = 2, max_budget: int = 5) -> None:
        self._initial = initial
        self._max = max_budget
        self._credits: dict[str, int] = {a: initial for a in agents}
        self._total_used: dict[str, int] = {a: 0 for a in agents}

    def can_search(self, agent_name: str) -> bool:
        """Check if agent has credits remaining."""
        return self._credits.get(agent_name, 0) > 0

    def consume(self, agent_name: str) -> bool:
        """Consume 1 search credit. Returns False if none left."""
        if self._credits.get(agent_name, 0) <= 0:
            return False
        self._credits[agent_name] -= 1
        self._total_used[agent_name] = self._total_used.get(agent_name, 0) + 1
        return True

    def earn(self, agent_name: str) -> None:
        """Earn +1 credit from chatroom_send (capped at max)."""
        current = self._credits.get(agent_name, 0)
        if current < self._max:
            self._credits[agent_name] = current + 1

    def status(self, agent_name: str) -> str:
        """Return credit status string for agent."""
        return f"{self._credits.get(agent_name, 0)}/{self._max}"

    def credits(self, agent_name: str) -> int:
        return self._credits.get(agent_name, 0)


class CachedSearchTool(Tool):
    """Wrapper around WebSearchTool with cross-agent dedup cache.

    All agents in a broadcast round share a single cache dict.
    If agent B searches the same query agent A already searched,
    B gets cached results + a hint to try different keywords.
    """

    name = "web_search"

    def __init__(self, original: Tool, agent_name: str, cache: dict, budget: SearchBudget | None = None) -> None:
        self._original = original
        self._agent_name = agent_name
        self._cache = cache
        self._budget = budget

    @property
    def description(self):
        return self._original.description

    @property
    def parameters(self):
        return self._original.parameters

    @staticmethod
    def _normalize_query(q: str) -> str:
        """Normalize query for cache lookup (lowercase, strip, collapse spaces)."""
        return re.sub(r'\s+', ' ', q.lower().strip())

    async def execute(self, **kwargs):
        query = kwargs.get("query", "")
        norm_q = self._normalize_query(query)

        # Check cache for exact or near-duplicate match
        if norm_q in self._cache:
            cached_result, searcher = self._cache[norm_q]
            return (
                f"[CACHED] {searcher} 已经搜过相同的关键词。结果如下：\n"
                f"{cached_result}\n\n"
                f"💡 请使用不同的关键词、角度或语言来搜索，"
                f"避免重复劳动。"
            )

        # Check search budget
        if self._budget and not self._budget.consume(self._agent_name):
            return (
                f"BLOCKED: search budget exhausted "
                f"({self._budget.status(self._agent_name)} credits). "
                f"Use chatroom_send to earn +1 credit, then search again."
            )

        # Execute real search
        result = await self._original.execute(**kwargs)

        # Cache the result
        self._cache[norm_q] = (result, self._agent_name)

        # Show remaining credits
        if self._budget:
            result += f"\n[search credits: {self._budget.status(self._agent_name)}]"
        return result


class ChatroomSendTool(Tool):
    """Send a message to one or more agents in the group chat.

    Supports sending to specific agents by name, broadcasting
    to all agents with ``"All"``, or sending a summary to the
    user with ``"User"``.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "", pool: ConversationPool | None = None, search_budget: SearchBudget | None = None) -> None:
        self._mailbox = mailbox
        self._agent_name = agent_name  # Set per-round by the engine
        self._pool = pool
        self._search_budget = search_budget
        self._last_received_from: str | None = None  # track who we last received from

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

        # Earn +1 search credit for participating in conversation
        if self._search_budget:
            self._search_budget.earn(self._agent_name)

        avail_hint = ""
        if self._pool:
            avail_hint = f" [{self._pool.used}/{self._pool.capacity} threads]"
        target_str = ", ".join(targets)
        return f"✅ sent to {target_str} ({delivered} delivered){avail_hint}"


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
