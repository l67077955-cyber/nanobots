"""Chatroom communication tools for inter-agent messaging.

Provides ``chatroom_send`` and ``wait`` tools that agents use
to communicate with each other during broadcast group chat rounds.
Also contains ``CachedSearchTool`` with search-tree resource management.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool, SpeakQueue


class SearchPool:
    """Per-agent search credit pool for broadcast mode.

    - Each agent gets individual credits (initial_per_agent)
    - Each search costs 1 credit from the agent's own quota
    - Every N outputs by an agent earns 1 credit back for that agent
    - Leader can transfer credits between agents via transfer()
    """

    def __init__(self, agents: list[str], initial_per_agent: int = 2,
                 earn_interval: int = 4) -> None:
        self._agents = agents
        self._initial = initial_per_agent
        self._earn_interval = earn_interval
        self._lock = threading.Lock()
        # Per-agent quotas
        self._credits: dict[str, int] = {a: initial_per_agent for a in agents}
        self._searches: dict[str, int] = {a: 0 for a in agents}
        self._outputs: dict[str, int] = {a: 0 for a in agents}

    @property
    def pool(self) -> int:
        """Total remaining credits across all agents."""
        return sum(self._credits.values())

    @property
    def total(self) -> int:
        return self._initial * len(self._agents)

    def spend(self, agent: str) -> bool:
        """Spend 1 credit from agent's own quota. Returns False if empty."""
        with self._lock:
            if self._credits.get(agent, 0) <= 0:
                return False
            self._credits[agent] -= 1
            self._searches[agent] = self._searches.get(agent, 0) + 1
            return True

    def on_output(self, agent: str) -> None:
        """Record an agent output. Every earn_interval outputs earns +1 credit for that agent."""
        with self._lock:
            self._outputs[agent] = self._outputs.get(agent, 0) + 1
            if self._outputs[agent] % self._earn_interval == 0:
                self._credits[agent] = self._credits.get(agent, 0) + 1

    def transfer(self, from_agent: str, to_agent: str, amount: int) -> tuple[bool, str]:
        """Transfer credits from one agent to another. Returns (success, message)."""
        with self._lock:
            if from_agent not in self._credits:
                return False, f"Agent '{from_agent}' 不存在"
            if to_agent not in self._credits:
                return False, f"Agent '{to_agent}' 不存在"
            available = self._credits[from_agent]
            if amount <= 0:
                return False, "转移数量必须大于0"
            actual = min(amount, available)
            if actual == 0:
                return False, f"{from_agent} 没有可用额度"
            self._credits[from_agent] -= actual
            self._credits[to_agent] += actual
            return True, f"✅ 转移 {actual} 额度: {from_agent}({self._credits[from_agent]}) → {to_agent}({self._credits[to_agent]})"

    def agent_credits(self, agent: str) -> int:
        """Remaining credits for this agent."""
        return self._credits.get(agent, 0)

    def agent_searches(self, agent: str) -> int:
        """How many searches this agent has done."""
        return self._searches.get(agent, 0)

    def status(self) -> str:
        """Return pool status string with per-agent breakdown."""
        parts = []
        for a in self._agents:
            c = self._credits.get(a, 0)
            s = self._searches.get(a, 0)
            parts.append(f"{a}:{c}💰({s}搜)")
        return " | ".join(parts)

    def agent_status(self, agent: str) -> str:
        """Return status for a single agent."""
        c = self._credits.get(agent, 0)
        s = self._searches.get(agent, 0)
        return f"{c} credits remaining ({s} searches used)"


class CachedSearchTool(Tool):
    """Wrapper around WebSearchTool with search-pool resource management.

    All agents share a SearchPool and a deduplication cache.
    Each search costs 1 credit from the pool.
    Supports batch search via the ``queries`` parameter (concurrent execution).
    """

    name = "web_search"

    def __init__(self, original: Tool, agent_name: str, cache: dict,
                 search_pool: SearchPool | None = None) -> None:
        self._original = original
        self._agent_name = agent_name
        self._cache = cache
        self._pool = search_pool
        # Per-iteration batch tracking: concurrent calls share 1 credit
        self._batch_lock = asyncio.Lock()
        self._batch_spent = False

    @property
    def description(self):
        base = self._original.description
        batch_hint = " Pass multiple queries as a list to search them all in parallel."
        if self._pool:
            return (
                f"{base}{batch_hint} "
                "Each agent has individual search credits. "
                "Credits regenerate when you produce output."
            )
        return f"{base}{batch_hint}"

    @property
    def parameters(self):
        orig = self._original.parameters
        return {
            "type": "object",
            "properties": {
                "query": {
                    "description": "Single search query (use 'queries' for batch)",
                    **{k: v for k, v in orig.get("properties", {}).get("query", {}).items()},
                },
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple search queries to run in parallel (batch mode)",
                },
                "count": orig.get("properties", {}).get("count", {"type": "integer"}),
            },
            "required": [],
        }

    @staticmethod
    def _normalize_query(q: str) -> str:
        """Normalize query for cache lookup (lowercase, strip, collapse spaces)."""
        return re.sub(r'\s+', ' ', q.lower().strip())

    async def _search_one(self, query: str, count: int | None, *, skip_pool: bool = False) -> str:
        """Search a single query, respecting cache and pool."""
        norm_q = self._normalize_query(query)

        if norm_q in self._cache:
            cached_result, searcher = self._cache[norm_q]
            pool_hint = f"\n\n[search pool: {self._pool.status()}]" if self._pool else ""
            return (
                f"[CACHED] {searcher} 已经搜过相同的关键词。结果如下：\n"
                f"{cached_result}\n\n"
                f"💡 请使用不同的关键词、角度或语言来搜索，"
                f"避免重复劳动。{pool_hint}"
            )

        if self._pool and not skip_pool:
            # Use batch lock: only first concurrent call spends a credit
            async with self._batch_lock:
                if not self._batch_spent:
                    if not self._pool.spend(self._agent_name):
                        return (
                            f"BLOCKED: 你的搜索额度用完了 "
                            f"({self._pool.agent_status(self._agent_name)})。\n"
                            f"先产出一些分析结果，额度会自动恢复。\n"
                            f"或请求 Leader 从其他 agent 划拨额度给你。"
                        )
                    self._batch_spent = True
                # Subsequent concurrent calls skip spending

        kwargs: dict = {"query": query}
        if count is not None:
            kwargs["count"] = count
        result = await self._original.execute(**kwargs)
        self._cache[norm_q] = (result, self._agent_name)

        if self._pool:
            result += f"\n[search pool: {self._pool.status()}]"
        return result

    async def execute(self, query: str = "", queries: list | None = None,
                      count: int | None = None, **kwargs):
        import asyncio as _asyncio

        # Batch mode: spend only 1 credit for the entire batch
        if queries:
            all_queries = list(queries)
            if query and query not in all_queries:
                all_queries.insert(0, query)

            # Spend 1 credit for the whole batch (not per-query)
            if self._pool:
                if not self._pool.spend(self._agent_name):
                    return (
                        f"BLOCKED: 你的搜索额度用完了 "
                        f"({self._pool.agent_status(self._agent_name)})。\n"
                        f"先产出一些分析结果，额度会自动恢复。\n"
                        f"或请求 Leader 从其他 agent 划拨额度给你。"
                    )

            tasks = [self._search_one(q, count, skip_pool=True) for q in all_queries]
            results = await _asyncio.gather(*tasks)
            parts = []
            for q, r in zip(all_queries, results):
                parts.append(f"=== Query: {q} ===\n{r}")
            return "\n\n".join(parts)

        # Single mode (original behaviour)
        if not query:
            return "Error: 必须提供 query 或 queries 参数"
        result = await self._search_one(query, count)
        # Reset batch flag after single call completes
        self._batch_spent = False
        return result


class SmartSearchTool(Tool):
    """Wrapper around any search tool that summarizes long results via a cheap LLM.

    When search results exceed a character threshold, they are summarized
    by a cheap model (e.g. gpt-4.1-nano) to reduce context consumption.
    Short results are passed through as-is.
    """

    name = "web_search"
    SUMMARIZE_THRESHOLD = 3000  # chars

    def __init__(self, original: Tool, reader_model: str = "openai/gpt-4.1-nano",
                 provider: Any = None) -> None:
        self._original = original
        self._reader_model = reader_model
        self._provider = provider

    @property
    def description(self):
        return self._original.description

    @property
    def parameters(self):
        return self._original.parameters

    async def execute(self, **kwargs) -> str:
        result = await self._original.execute(**kwargs)

        if len(result) <= self.SUMMARIZE_THRESHOLD:
            return result

        # Build query context for targeted summarization
        queries = kwargs.get("queries") or []
        q = kwargs.get("query", "")
        if q and q not in queries:
            queries = [q] + list(queries)
        query_context = " / ".join(queries) if queries else ""

        # Summarize long results
        try:
            summary = await self._summarize(result, query_context)
            if summary:
                return summary
        except Exception as e:
            from loguru import logger
            logger.warning("SmartSearch: summarization failed, returning raw: {}", e)

        return result

    async def _summarize(self, raw_results: str, query_context: str = "") -> str | None:
        """Summarize search results using a cheap LLM."""
        from loguru import logger

        reader_cfg = SmartFetchTool._load_reader_config()
        model = reader_cfg.get("model", self._reader_model)
        provider_name = reader_cfg.get("provider", "openrouter")

        from nanobot.providers.litellm_provider import LiteLLMProvider
        import json as _json

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

        # Build context-aware prompt
        context_hint = ""
        if query_context:
            context_hint = (
                f"用户搜索意图：「{query_context}」\n"
                "请围绕这个搜索意图提取最相关的信息，忽略无关结果。\n\n"
            )

        prompt = (
            "你是搜索结果摘要助手。严格遵守以下规则：\n"
            "1. 只输出搜索结果中 **已有** 的信息，绝对不要添加、推测或编造任何内容。\n"
            "2. 每条事实必须标注来源编号（如 [1], [2]），对应原始搜索结果的序号。\n"
            "3. 完整保留所有 URL 链接，不要省略或改写。\n"
            "4. 关键数据（数字、日期、版本号、人名）必须原文照抄，不得改写。\n"
            "5. 用与原始结果相同的语言输出（英文结果用英文，中文结果用中文）。\n"
            "6. 如果信息相互矛盾，保留所有版本并标注各自来源。\n"
            "7. 去掉重复内容，合并同一来源的信息。\n\n"
            f"{context_hint}"
            f"--- 搜索结果 ({len(raw_results)}字) ---\n"
            f"{raw_results[:8000]}\n--- 结束 ---"
        )

        logger.info("SmartSearch: summarizing {}c via {}/{} (query={})",
                     len(raw_results), provider_name, model, query_context[:50])
        response = await llm.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.1,
        )
        summary = response.content.strip() if response.content else None
        if summary:
            usage = response.usage or {}
            nano_p = usage.get("prompt_tokens", 0)
            nano_c = usage.get("completion_tokens", 0)
            nano_t = usage.get("total_tokens", 0)
            saved = len(raw_results) - len(summary)
            pct = round(saved / len(raw_results) * 100) if raw_results else 0
            tok_info = f" | nano in:{nano_p} out:{nano_c} Σ{nano_t}" if nano_t else ""
            logger.info("SmartSearch: {}c → {}c -{}% (nano {}tok)", len(raw_results), len(summary), pct, nano_t)
            return (
                f"{summary}\n\n"
                f"`[nano:search] {len(raw_results)}→{len(summary)}c -{pct}%{tok_info}`"
            )
        return None


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
                 provider: Any = None, max_extract_chars: int = 12000) -> None:
        self._original = original
        self._reader_model = reader_model
        self._provider = provider  # LiteLLMProvider instance
        self._max_extract_chars = max_extract_chars

    @property
    def parameters(self):
        base = self._original.parameters
        props = dict(base.get("properties", {}))
        props["focus"] = {
            "type": "string",
            "description": (
                "Optional: specify what to focus on during content extraction. "
                "E.g. keywords, topics, or specific data you want."
            ),
        }
        return {**base, "properties": props}

    async def execute(self, **kwargs) -> str:
        import json as _json

        focus = kwargs.pop("focus", "")

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
            extracted, nano_usage = await self._extract_content(url, raw_text, focus=focus)
            if extracted:
                nano_p = nano_usage.get("prompt_tokens", 0)
                nano_c = nano_usage.get("completion_tokens", 0)
                nano_t = nano_usage.get("total_tokens", 0)
                saved = len(raw_text) - len(extracted)
                pct = round(saved / len(raw_text) * 100) if raw_text else 0
                tok_info = f" | nano in:{nano_p} out:{nano_c} Σ{nano_t}" if nano_t else ""
                focus_label = f" focus=\"{focus[:30]}\"" if focus else ""
                extracted += f"\n\n`[nano:fetch] {len(raw_text)}→{len(extracted)}c -{pct}%{focus_label}{tok_info}`"
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

    async def _extract_content(self, url: str, raw_text: str,
                               focus: str = "") -> tuple[str | None, dict]:
        """Use cheap LLM to extract key content from raw fetched text.

        Returns (extracted_text, usage_dict).
        """
        from loguru import logger

        # Truncate input to avoid excessive token usage
        input_text = raw_text[:self._max_extract_chars]

        # Build context-aware extraction prompt
        focus_hint = ""
        if focus:
            focus_hint = (
                f"提取重点：「{focus}」\n"
                "请围绕上述重点提取最相关的内容，其他信息可简略。\n\n"
            )

        prompt = (
            "你是一个网页内容提取助手。严格遵守以下规则：\n"
            "1. 只提取网页中 **已有** 的信息，绝对不要添加、推测或编造。\n"
            "2. 关键数据（数字、日期、代码、人名、专有名词）必须原文照抄。\n"
            "3. 保留所有重要的链接 URL。\n"
            "4. 去掉导航菜单、广告、页脚、cookie 提示等无关内容。\n"
            "5. 用与原文相同的语言输出（英文网页用英文，中文网页用中文）。\n"
            "6. 如果不确定某信息是否重要，保留它。\n\n"
            f"{focus_hint}"
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

        focus_log = f" focus='{focus[:40]}'" if focus else ""
        logger.info("SmartFetch: extracting {} via {}/{} (input={}c{})",
                     url[:60], provider_name, model, len(input_text), focus_log)
        response = await llm.chat(messages, max_tokens=4000, temperature=0.1)
        usage = response.usage or {}
        result = response.content.strip() if response.content else None
        if result:
            logger.info("SmartFetch: extracted {}c from {}c (nano {}tok)",
                        len(result), len(input_text), usage.get("total_tokens", 0))
        return result, usage

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


class LeaderGate:
    """Enforces leader-gated speaking order.

    Each non-leader agent may send at most 1 message between consecutive
    leader messages.  When the leader sends a message, all counters reset.
    """

    def __init__(self, leader_name: str) -> None:
        self._leader = leader_name
        # {agent_name: sends_since_leader_spoke}
        self._counts: dict[str, int] = {}

    def try_send(self, agent_name: str) -> bool:
        """Return True if the agent is allowed to send."""
        if agent_name == self._leader:
            return True
        return self._counts.get(agent_name, 0) < 1

    def record_send(self, agent_name: str) -> None:
        """Record that agent sent a message."""
        if agent_name == self._leader:
            # Leader spoke — reset everyone's counter
            for k in self._counts:
                self._counts[k] = 0
        else:
            self._counts[agent_name] = self._counts.get(agent_name, 0) + 1

    @property
    def leader(self) -> str:
        return self._leader

class ChatroomSendTool(Tool):
    """Send a message to one or more agents in the group chat.

    Supports sending to specific agents by name, broadcasting
    to all agents with ``"All"``, or sending a summary to the
    user with ``"User"``.
    """

    def __init__(self, mailbox: MailboxHub, agent_name: str = "", pool: ConversationPool | None = None,
                 search_pool: "SearchPool | None" = None,
                 leader_gate: LeaderGate | None = None) -> None:
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


class ManageAgentTool(Tool):
    """Leader-only tool: manage agents during broadcast execution.

    Actions:
        disable   — Remove an agent from the current round (cancels its task)
        enable    — Mark a disabled agent as active again (no task restart)
        restart   — Re-spawn a disabled agent's task so it actively participates
        set_tools — Change an agent's tool permissions for this session
        set_status — Modify the agent's status message injected into its next cycle
    """

    def __init__(
        self,
        *,
        exec_agents: list[str],
        agent_tasks: dict,  # asyncio.Task → name mapping
        engine: Any,
        mailbox: Any,
        spawn_fn: Any = None,  # Callable[[str, int], asyncio.Task] | None
    ) -> None:
        self._exec_agents = exec_agents
        self._agent_tasks = agent_tasks  # {Task: name}
        self._engine = engine
        self._mailbox = mailbox
        self._spawn_fn = spawn_fn  # injected by broadcast_round
        self._disabled: set[str] = set()
        # {agent_name: status_message} — injected into agent's next cycle
        self._status_overrides: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "manage_agent"

    @property
    def description(self) -> str:
        return (
            "管理 agent: disable(踢出), restart(拉回重启), enable(标记激活), "
            "set_tools(改工具权限), set_status(修改agent状态消息)。仅当前轮有效。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["disable", "enable", "restart", "set_tools", "set_status"],
                    "description": (
                        "操作类型: "
                        "disable=踢出并取消任务, "
                        "restart=重新拉回并启动任务, "
                        "enable=仅标记为激活(不重启任务), "
                        "set_tools=修改工具权限, "
                        "set_status=注入状态消息到agent下次循环"
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": "目标 agent 名字",
                },
                "tools": {
                    "type": "object",
                    "description": "工具权限 (仅 set_tools 时需要), 如 {\"web_search\": true, \"exec\": false}",
                },
                "status": {
                    "type": "string",
                    "description": "状态消息 (仅 set_status 时需要), 会作为系统提示注入agent下次循环",
                },
            },
            "required": ["action", "agent"],
        }

    async def execute(
        self,
        action: str = "",
        agent: str = "",
        tools: dict | None = None,
        status: str = "",
        **kwargs: Any,
    ) -> str:
        if not action or not agent:
            return "Error: 必须指定 action 和 agent"

        if agent not in self._exec_agents:
            return f"Error: agent '{agent}' 不在当前轮中。可用: {', '.join(self._exec_agents)}"

        if action == "disable":
            if agent in self._disabled:
                return f"{agent} 已经被 disable 了"
            self._disabled.add(agent)
            # Cancel the agent's task
            for task_obj, task_name in self._agent_tasks.items():
                if task_name == agent and not task_obj.done():
                    task_obj.cancel()
                    break
            # Notify remaining active agents
            active = [a for a in self._exec_agents if a not in self._disabled]
            if active:
                self._mailbox.send("系统", active,
                    f"[系统通知] {agent} 已被 Leader 移除本轮讨论")
            await self._engine._send(f"⛔ Leader 已移除 {agent}")
            return f"✅ {agent} 已被 disable，其 task 已取消"

        elif action == "restart":
            # Re-spawn the agent's task so it actively participates again
            if self._spawn_fn is None:
                return f"Error: restart 不可用（spawn_fn 未注入）"
            # Mark as active first
            self._disabled.discard(agent)
            # Cancel any existing (zombie) task for this agent
            for task_obj, task_name in list(self._agent_tasks.items()):
                if task_name == agent and not task_obj.done():
                    task_obj.cancel()
            # Determine agent index in exec_agents list
            idx = self._exec_agents.index(agent) if agent in self._exec_agents else 0
            # Notify agent it's being restarted
            notify_msg = f"[系统通知] Leader 已将你（{agent}）拉回讨论，请重新开始参与。"
            self._mailbox.send("系统", [agent], notify_msg)
            # Spawn new task
            new_task = self._spawn_fn(agent, idx)
            self._agent_tasks[new_task] = agent
            # Notify others
            active = [a for a in self._exec_agents if a not in self._disabled]
            others = [a for a in active if a != agent]
            if others:
                self._mailbox.send("系统", others,
                    f"[系统通知] {agent} 已被 Leader 拉回，重新加入讨论")
            await self._engine._send(f"🔄 Leader 已重启 {agent}")
            return f"✅ {agent} 已重新启动，新 task 已创建"

        elif action == "enable":
            if agent not in self._disabled:
                return f"{agent} 没有被 disable（当前已是激活状态）"
            self._disabled.discard(agent)
            # Notify
            active = [a for a in self._exec_agents if a not in self._disabled]
            if active:
                self._mailbox.send("系统", active,
                    f"[系统通知] {agent} 已被 Leader 标记为激活")
            await self._engine._send(f"✅ Leader 已激活 {agent}（标记，未重启任务）")
            return f"✅ {agent} 已标记为 enable。如需重新参与讨论请用 restart。"

        elif action == "set_tools":
            if not tools or not isinstance(tools, dict):
                return "Error: set_tools 需要 tools 参数，如 {\"web_search\": true}"
            cfg = self._engine.registry.get(agent, {})
            current = cfg.get("tools", {})
            if isinstance(current, dict):
                current.update(tools)
            else:
                cfg["tools"] = dict(tools)
            # Notify
            active = [a for a in self._exec_agents if a not in self._disabled]
            changes = ", ".join(f"{k}={'开' if v else '关'}" for k, v in tools.items())
            if active:
                self._mailbox.send("系统", active,
                    f"[系统通知] Leader 已修改 {agent} 的工具权限: {changes}")
            await self._engine._send(f"🔧 Leader 修改 {agent} 权限: {changes}")
            return f"✅ {agent} 工具权限已更新: {changes}"

        elif action == "set_status":
            if not status:
                return "Error: set_status 需要 status 参数"
            self._status_overrides[agent] = status
            # Send status message directly to the agent's mailbox so it sees it on next wait()
            self._mailbox.send("系统", [agent],
                f"[Leader 状态更新] {status}")
            await self._engine._send(f"📋 Leader 已更新 {agent} 状态: {status[:80]}")
            return f"✅ {agent} 状态已更新，消息已注入其收件箱"

        return f"Error: 未知 action '{action}'"


class EndDiscussionTool(Tool):
    """Leader-only tool: end the discussion phase immediately.

    When called, sets an asyncio.Event that the broadcast loop watches.
    All agent tasks are cancelled and the leader enters the synthesis phase.
    """

    def __init__(self, *, end_event: Any, engine: Any) -> None:
        self._end_event = end_event  # asyncio.Event
        self._engine = engine

    @property
    def name(self) -> str:
        return "end_discussion"

    @property
    def description(self) -> str:
        return (
            "结束当前讨论，立即进入总结阶段。"
            "当你判断信息已经足够、讨论陷入循环、或 agent 表现不佳时使用。"
            "调用后所有 agent 会被停止，你将进入最终总结。"
            "参数: reason (可选，结束原因)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "结束讨论的原因（可选）",
                },
            },
            "required": [],
        }

    async def execute(self, reason: str = "", **kwargs: Any) -> str:
        reason_str = f"（原因: {reason}）" if reason else ""
        await self._engine._send(f"★ Leader 决定结束讨论{reason_str}")
        self._end_event.set()
        return f"✅ 讨论已结束{reason_str}，即将进入总结阶段"


class ClearContextTool(Tool):
    """Leader-only tool: clear an agent's context from the shared history.

    Removes all messages sent by the target agent from the engine's history,
    then injects a system notification into the agent's mailbox so it knows
    its context has been reset and it should start fresh.
    """

    def __init__(self, *, engine: Any, mailbox: Any, exec_agents: list[str]) -> None:
        self._engine = engine
        self._mailbox = mailbox
        self._exec_agents = exec_agents

    @property
    def name(self) -> str:
        return "clear_context"

    @property
    def description(self) -> str:
        return (
            "清理 agent 的上下文：从共享历史中移除指定 agent 的所有消息，"
            "并通知该 agent 重新开始。适用于某个 agent 陷入循环或输出垃圾时。"
            "参数: agent (目标agent), keep_last (保留最后N条消息，默认0=全清)"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "要清理上下文的 agent 名字",
                },
                "keep_last": {
                    "type": "integer",
                    "description": "保留该 agent 最后 N 条消息（0=全清，默认0）",
                },
                "reason": {
                    "type": "string",
                    "description": "清理原因（会通知给该 agent）",
                },
            },
            "required": ["agent"],
        }

    async def execute(
        self,
        agent: str = "",
        keep_last: int = 0,
        reason: str = "",
        **kwargs: Any,
    ) -> str:
        if not agent:
            return "Error: 必须指定 agent"

        if agent not in self._exec_agents:
            return f"Error: agent '{agent}' 不在当前轮中。可用: {', '.join(self._exec_agents)}"

        history = self._engine._history
        # Collect messages from this agent
        agent_msgs = [m for m in history if m.get("sender") == agent]
        total = len(agent_msgs)

        if total == 0:
            return f"⚠️ {agent} 在历史中没有消息，无需清理"

        # Determine how many to remove
        remove_count = max(0, total - keep_last)
        if remove_count == 0:
            return f"⚠️ keep_last={keep_last} 已覆盖所有消息，无消息被清理"

        # Remove oldest remove_count messages from this agent
        removed = 0
        new_history = []
        agent_seen = 0
        for m in history:
            if m.get("sender") == agent:
                agent_seen += 1
                if agent_seen <= remove_count:
                    removed += 1
                    continue  # drop this message
            new_history.append(m)

        self._engine._history[:] = new_history

        # Notify the agent via mailbox
        reason_str = f"（原因: {reason}）" if reason else ""
        notify = (
            f"[系统通知] Leader 已清理你（{agent}）的上下文历史{reason_str}。"
            f"共清理 {removed} 条消息。请忘记之前的思路，重新分析当前任务。"
        )
        self._mailbox.send("系统", [agent], notify)

        keep_info = f"，保留最后 {keep_last} 条" if keep_last > 0 else ""
        await self._engine._send(
            f"🧹 Leader 已清理 {agent} 的上下文（{removed}/{total} 条{keep_info}）"
        )
        return f"✅ 已清理 {agent} 的 {removed} 条历史消息{keep_info}，已通知该 agent 重置思路"


class TransferCreditsTool(Tool):
    """Leader-only tool: transfer search credits between agents."""

    def __init__(self, *, search_pool: SearchPool, engine: Any) -> None:
        self._pool = search_pool
        self._engine = engine

    @property
    def name(self) -> str:
        return "transfer_credits"

    @property
    def description(self) -> str:
        return (
            "划拨搜索额度：把一个 agent 的搜索额度转给另一个 agent。"
            "例如把没有搜索工具的 agent 的额度划给有搜索能力的 agent。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "from_agent": {
                    "type": "string",
                    "description": "从哪个 agent 划出额度",
                },
                "to_agent": {
                    "type": "string",
                    "description": "划给哪个 agent",
                },
                "amount": {
                    "type": "integer",
                    "description": "划拨数量（如不确定可填大数，系统会自动取可用最大值）",
                },
            },
            "required": ["from_agent", "to_agent", "amount"],
        }

    async def execute(self, from_agent: str = "", to_agent: str = "",
                      amount: int = 0, **kwargs: Any) -> str:
        if not from_agent or not to_agent:
            return "Error: 必须指定 from_agent 和 to_agent"
        success, msg = self._pool.transfer(from_agent, to_agent, amount)
        if success:
            await self._engine._send(f"🔄 {msg}")
            return f"{msg}\n当前额度: {self._pool.status()}"
        return f"Error: {msg}"
