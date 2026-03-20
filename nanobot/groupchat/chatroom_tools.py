"""Chatroom communication tools for inter-agent messaging.

Provides ``chatroom_send`` and ``wait`` tools that agents use
to communicate with each other during broadcast group chat rounds.
Also contains ``CachedSearchTool`` with search-tree resource management.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool, SpeakQueue


class SearchPool:
    """Shared search credit pool for broadcast mode.

    - Each search costs 1 credit (spend)
    - Every N agent outputs earns 1 credit back (earn_interval)
    - Pool starts at initial_per_agent × n_agents
    """

    def __init__(self, agents: list[str], initial_per_agent: int = 2,
                 earn_interval: int = 4) -> None:
        self._agents = agents
        self._total = initial_per_agent * len(agents)
        self._pool = self._total
        self._earn_interval = earn_interval
        self._output_count = 0  # total outputs across all agents
        self._lock = threading.Lock()
        self._searches: dict[str, int] = {a: 0 for a in agents}

    @property
    def pool(self) -> int:
        return self._pool

    @property
    def total(self) -> int:
        return self._total

    def spend(self, agent: str) -> bool:
        """Spend 1 credit for a search. Returns False if pool empty."""
        with self._lock:
            if self._pool <= 0:
                return False
            self._pool -= 1
            self._searches[agent] = self._searches.get(agent, 0) + 1
            return True

    def on_output(self, agent: str) -> None:
        """Record an agent output. Every earn_interval outputs earns +1 credit."""
        with self._lock:
            self._output_count += 1
            if self._output_count % self._earn_interval == 0:
                self._pool = min(self._pool + 1, self._total)

    def agent_searches(self, agent: str) -> int:
        """How many searches this agent has done."""
        return self._searches.get(agent, 0)

    def status(self) -> str:
        """Return pool status string."""
        total_searches = sum(self._searches.values())
        return f"{self._pool}/{self._total} ({total_searches} searches)"


class CachedSearchTool(Tool):
    """Wrapper around WebSearchTool with search-pool resource management.

    All agents share a SearchPool and a deduplication cache.
    Each search costs 1 credit from the pool.
    """

    name = "web_search"

    def __init__(self, original: Tool, agent_name: str, cache: dict,
                 search_pool: SearchPool | None = None) -> None:
        self._original = original
        self._agent_name = agent_name
        self._cache = cache
        self._pool = search_pool

    @property
    def description(self):
        base = self._original.description
        if self._pool:
            return (
                f"{base} "
                "Searches cost credits from a shared pool. "
                "Credits regenerate when agents produce output."
            )
        return base

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
            pool_hint = f"\n\n[search pool: {self._pool.status()}]" if self._pool else ""
            return (
                f"[CACHED] {searcher} 已经搜过相同的关键词。结果如下：\n"
                f"{cached_result}\n\n"
                f"💡 请使用不同的关键词、角度或语言来搜索，"
                f"避免重复劳动。{pool_hint}"
            )

        # Check search pool budget
        if self._pool:
            if not self._pool.spend(self._agent_name):
                return (
                    f"BLOCKED: 搜索额度用完了 "
                    f"({self._pool.status()})。\n"
                    f"先产出一些分析结果，额度会自动恢复。"
                )

        # Execute real search
        result = await self._original.execute(**kwargs)

        # Cache the result
        self._cache[norm_q] = (result, self._agent_name)

        # Append pool status hint
        if self._pool:
            result += f"\n[search pool: {self._pool.status()}]"
        return result


class SmartFetchTool(Tool):
    """Wrapper around WebFetchTool that uses a cheap model to extract key content.

    Flow: fetch URL → pass raw text to cheap LLM → return clean extraction.
    Falls back to raw content if LLM call fails.
    """

    name = "web_fetch"
    description = (
        "Fetch URL and extract readable content. "
        "Content is automatically processed by an AI reader for clean extraction."
    )

    def __init__(self, original: Tool, reader_model: str = "openai/gpt-4.1-nano",
                 provider: Any = None, max_extract_chars: int = 30000) -> None:
        self._original = original
        self._reader_model = reader_model
        self._provider = provider  # LiteLLMProvider instance
        self._max_extract_chars = max_extract_chars

    @property
    def parameters(self):
        return self._original.parameters

    async def execute(self, **kwargs) -> str:
        import json as _json

        # Step 1: Fetch raw content
        raw_result = await self._original.execute(**kwargs)

        # Parse the JSON result from WebFetchTool
        try:
            data = _json.loads(raw_result)
        except (ValueError, TypeError):
            return raw_result  # Not JSON, return as-is

        if "error" in data:
            return raw_result  # Error response, pass through

        raw_text = data.get("text", "")
        url = data.get("url", kwargs.get("url", ""))

        if not raw_text or len(raw_text) < 50:
            return raw_result  # Too short to process

        # Step 2: Extract via cheap model
        try:
            extracted = await self._extract_content(url, raw_text)
            if extracted:
                result_data = {
                    "url": url,
                    "finalUrl": data.get("finalUrl", url),
                    "status": data.get("status", 200),
                    "extractor": "ai_reader",
                    "reader_model": self._reader_model,
                    "truncated": data.get("truncated", False),
                    "original_length": len(raw_text),
                    "extracted_length": len(extracted),
                    "text": extracted,
                }
                return _json.dumps(result_data, ensure_ascii=False)
        except Exception as e:
            from loguru import logger
            logger.warning("SmartFetch: AI extraction failed for {}: {}", url, e)

        # Fallback: return raw content
        return raw_result

    async def _extract_content(self, url: str, raw_text: str) -> str | None:
        """Use cheap LLM to extract key content from raw fetched text."""
        from loguru import logger

        # Truncate input to avoid excessive token usage
        input_text = raw_text[:self._max_extract_chars]

        prompt = (
            "你是一个网页内容提取助手。请从以下网页内容中提取关键信息，"
            "保留所有重要的事实、数据、日期、链接和结论。"
            "去掉导航菜单、广告、页脚等无关内容。"
            "用简洁清晰的中文输出，保持原文的关键细节。\n\n"
            f"URL: {url}\n\n"
            f"--- 网页内容 ---\n{input_text}\n--- 结束 ---"
        )

        messages = [{"role": "user", "content": prompt}]

        # Build a LiteLLMProvider from reader agent config
        reader_cfg = self._load_reader_config()
        model = reader_cfg.get("model", self._reader_model)
        provider_name = reader_cfg.get("provider", "openrouter")

        from nanobot.providers.litellm_provider import LiteLLMProvider
        import json as _json
        # Read provider credentials from nanobot config
        api_key, api_base = "", ""
        try:
            cfg_path = Path.home() / ".nanobot" / "config.json"
            if cfg_path.exists():
                cfg = _json.loads(cfg_path.read_text())
                pcfg = (cfg.get("providers") or {}).get(provider_name, {}) or {}
                api_key = pcfg.get("apiKey", "")
                api_base = pcfg.get("apiBase", "")
        except Exception:
            pass

        llm = LiteLLMProvider(
            default_model=model,
            api_key=api_key or None,
            api_base=api_base or None,
            provider_name=provider_name,
        )

        logger.info("SmartFetch: extracting {} via {}/{} (input={}c)",
                     url[:60], provider_name, model, len(input_text))
        response = await llm.chat(messages, max_tokens=4000, temperature=0.1)
        result = response.content.strip() if response.content else None
        if result:
            logger.info("SmartFetch: extracted {}c from {}c", len(result), len(input_text))
        return result

    @staticmethod
    def _load_reader_config() -> dict[str, Any]:
        """Load reader agent config from disk."""
        import json as _json
        cfg_path = Path.home() / ".nanobot" / "agents" / "reader" / "config.json"
        if cfg_path.exists():
            try:
                cfg = _json.loads(cfg_path.read_text())
                model = (
                    cfg.get("model")
                    or cfg.get("agents", {}).get("defaults", {}).get("model")
                )
                provider = (
                    cfg.get("provider")
                    or cfg.get("agents", {}).get("defaults", {}).get("provider")
                )
                result: dict[str, Any] = {}
                if model:
                    result["model"] = model
                if provider:
                    result["provider"] = provider
                return result
            except Exception:
                pass
        return {}


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
