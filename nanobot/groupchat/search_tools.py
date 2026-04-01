"""Search and content extraction tools for group chat.

Contains:
- ``SearchPool``: Per-agent search credit management
- ``CachedSearchTool``: Search with dedup cache + credit system
- ``SmartSearchTool``: Summarize long search results via cheap LLM
- ``SmartFetchTool``: AI-powered URL content extraction
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


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
        """Use cheap LLM to extract key content from raw fetched text."""
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
