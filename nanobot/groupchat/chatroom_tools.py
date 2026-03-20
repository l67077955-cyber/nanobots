"""Chatroom communication tools for inter-agent messaging.

Provides ``chatroom_send`` and ``wait`` tools that agents use
to communicate with each other during broadcast group chat rounds.
Also contains ``CachedSearchTool`` with search-tree resource management.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool, SpeakQueue


class SearchTree:
    """Shared search tree with point-pool resource management.

    All agents share a tree of search results and a shared point pool.
    Each search consumes 1 point and adds a child node to the tree.

    Refund rules (incentivize exploration over redundancy):
    - Hanging on a LEAF node (new direction): instant refund of `k` points
    - Hanging on a NON-LEAF node (already explored): refund after search completes
    """

    def __init__(self, agents: list[str], total: int | None = None, refund: int = 1) -> None:
        self._agents = agents
        self._total = total if total is not None else len(agents)
        self._refund = refund
        self._pool = self._total  # shared point pool
        self._lock = threading.Lock()
        # Tree: node 0 = root (no data)
        self._nodes: list[dict] = [{"id": 0, "query": "", "agent": "", "children": []}]
        self._next_id = 1

    @property
    def pool(self) -> int:
        return self._pool

    @property
    def total(self) -> int:
        return self._total

    def consume(self) -> bool:
        """Consume 1 point from shared pool. Returns False if empty."""
        with self._lock:
            if self._pool <= 0:
                return False
            self._pool -= 1
            return True

    def _refund_points(self) -> None:
        """Return k points to shared pool."""
        with self._lock:
            self._pool = min(self._pool + self._refund, self._total)

    def is_leaf(self, node_id: int) -> bool:
        """Check if a node is a leaf (no children)."""
        if 0 <= node_id < len(self._nodes):
            return len(self._nodes[node_id]["children"]) == 0
        return True

    def add_node(self, agent: str, query: str, parent_id: int = 0) -> int:
        """Add a search node to the tree. Returns the new node ID."""
        if parent_id < 0 or parent_id >= len(self._nodes):
            parent_id = 0
        new_id = self._next_id
        self._next_id += 1
        node = {"id": new_id, "query": query, "agent": agent, "children": []}
        self._nodes.append(node)
        self._nodes[parent_id]["children"].append(new_id)
        return new_id

    def pre_search(self, agent: str, query: str, parent_id: int = 0) -> tuple[bool, int, bool]:
        """Before executing search: consume point, check leaf, add node.

        Returns: (ok, node_id, is_leaf_parent)
        - ok: False if pool empty
        - node_id: the new node ID
        - is_leaf_parent: True if parent was a leaf (instant refund)
        """
        if not self.consume():
            return False, -1, False
        leaf = self.is_leaf(parent_id)
        node_id = self.add_node(agent, query, parent_id)
        if leaf:
            self._refund_points()  # instant refund for exploring new ground
        return True, node_id, leaf

    def post_search(self, node_id: int, was_leaf: bool) -> None:
        """After search completes: delayed refund for non-leaf branches."""
        if not was_leaf:
            self._refund_points()

    def status(self) -> str:
        """Return pool status string."""
        return f"{self._pool}/{self._total}"

    def tree_str(self, max_depth: int = 3) -> str:
        """Render tree as text for agent display."""
        lines: list[str] = []
        self._render(0, "", True, lines, 0, max_depth)
        return "\n".join(lines) if lines else "(empty)"

    def _render(self, node_id: int, prefix: str, is_last: bool,
                lines: list[str], depth: int, max_depth: int) -> None:
        if depth > max_depth:
            return
        node = self._nodes[node_id]
        if node_id == 0:
            lines.append("search_tree (root)")
        else:
            connector = "└── " if is_last else "├── "
            q_short = node["query"][:40]
            n_children = len(node["children"])
            suffix = f" ({n_children}↓)" if n_children > 0 else ""
            lines.append(f"{prefix}{connector}#{node_id} [{node['agent']}] \"{q_short}\"{suffix}")
        children = node["children"]
        for i, child_id in enumerate(children):
            child_prefix = prefix + ("    " if is_last else "│   ") if node_id != 0 else ""
            self._render(child_id, child_prefix, i == len(children) - 1,
                        lines, depth + 1, max_depth)


class CachedSearchTool(Tool):
    """Wrapper around WebSearchTool with search-tree resource management.

    All agents share a SearchTree. Each search adds a node to the tree.
    Agents specify a `parent` node to branch from (default: root).
    """

    name = "web_search"

    def __init__(self, original: Tool, agent_name: str, cache: dict,
                 search_tree: SearchTree | None = None) -> None:
        self._original = original
        self._agent_name = agent_name
        self._cache = cache
        self._tree = search_tree

    @property
    def description(self):
        base = self._original.description
        if self._tree:
            return (
                f"{base} "
                "Optional 'parent' param: node ID to branch from in the search tree. "
                "Default=0 (root, new topic). Set to a node ID to drill deeper into "
                "an existing search direction. Exploring new branches (leaf nodes) is "
                "cheaper than re-exploring existing ones."
            )
        return base

    @property
    def parameters(self):
        params = dict(self._original.parameters)
        if self._tree:
            props = dict(params.get("properties", {}))
            props["parent"] = {
                "type": "integer",
                "description": (
                    "Parent node ID in the search tree to branch from. "
                    "0 = root (new topic). Use a node ID to drill deeper. "
                    "Branching from leaf nodes gives instant point refund."
                ),
            }
            params["properties"] = props
        return params

    @staticmethod
    def _normalize_query(q: str) -> str:
        """Normalize query for cache lookup (lowercase, strip, collapse spaces)."""
        return re.sub(r'\s+', ' ', q.lower().strip())

    async def execute(self, **kwargs):
        query = kwargs.get("query", "")
        parent_id = int(kwargs.pop("parent", 0))
        norm_q = self._normalize_query(query)

        # Check cache for exact or near-duplicate match
        if norm_q in self._cache:
            cached_result, searcher = self._cache[norm_q]
            tree_hint = ""
            if self._tree:
                tree_hint = f"\n\n[search tree]\n{self._tree.tree_str()}\n[pool: {self._tree.status()}]"
            return (
                f"[CACHED] {searcher} 已经搜过相同的关键词。结果如下：\n"
                f"{cached_result}\n\n"
                f"💡 请使用不同的关键词、角度或语言来搜索，"
                f"避免重复劳动。{tree_hint}"
            )

        # Check search tree budget
        if self._tree:
            ok, node_id, was_leaf = self._tree.pre_search(
                self._agent_name, query, parent_id,
            )
            if not ok:
                return (
                    f"BLOCKED: search pool exhausted "
                    f"({self._tree.status()} points). "
                    f"Wait for teammates to finish their searches.\n\n"
                    f"[search tree]\n{self._tree.tree_str()}"
                )

        # Execute real search
        result = await self._original.execute(**kwargs)

        # Cache the result
        self._cache[norm_q] = (result, self._agent_name)

        # Post-search: delayed refund for non-leaf branches
        if self._tree:
            self._tree.post_search(node_id, was_leaf)
            refund_note = "⚡instant" if was_leaf else "✓delayed"
            result += (
                f"\n[search pool: {self._tree.status()} | "
                f"node #{node_id} on {'leaf' if was_leaf else 'branch'} → refund {refund_note}]"
                f"\n[search tree]\n{self._tree.tree_str()}"
            )
        return result


class ChatroomSendTool(Tool):
    """Send a message to one or more agents in the group chat.

    Supports sending to specific agents by name, broadcasting
    to all agents with ``"All"``, or sending a summary to the
    user with ``"User"``.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "", pool: ConversationPool | None = None) -> None:
        self._mailbox = mailbox
        self._agent_name = agent_name  # Set per-round by the engine
        self._pool = pool
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
