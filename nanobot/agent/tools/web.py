"""Web tools: web_search and web_fetch."""

import html
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _has_cjk(text: str) -> bool:
    """Return True if text contains CJK (Chinese/Japanese/Korean) characters."""
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text))


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """Search the web using DuckDuckGo Lite (no API key required)."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, and snippets. Use freshness='pd' for today's news."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10},
            "freshness": {"type": "string", "description": "Time filter: 'pd'=past day, 'pw'=past week, 'pm'=past month", "enum": ["pd", "pw", "pm"]}
        },
        "required": ["query"]
    }

    _DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"

    def __init__(self, api_key: str | None = None, max_results: int = 10, proxy: str | None = None):
        self._init_api_key = api_key  # kept for compat, not used by DDG
        self.max_results = max_results
        self.proxy = proxy

    @property
    def api_key(self) -> str:
        """Kept for backward compatibility."""
        return self._init_api_key or os.environ.get("BRAVE_API_KEY", "")

    async def execute(self, query: str, count: int | None = None, freshness: str | None = None, **kwargs: Any) -> str:
        try:
            n = min(max(count or self.max_results, 1), 10)

            # Append time hint to query for freshness filtering
            time_query = query
            if freshness == "pd":
                time_query = f"{query} 今天"
            elif freshness == "pw":
                time_query = f"{query} 本周"
            elif freshness == "pm":
                time_query = f"{query} 本月"

            # DuckDuckGo Lite HTML endpoint (works from servers, no API key)
            data = {"q": time_query}
            if _has_cjk(query):
                data["kl"] = "cn-zh"

            headers = {"User-Agent": USER_AGENT}
            logger.debug("WebSearch DDG: q={} (freshness={})", query[:50], freshness or "none")

            async with httpx.AsyncClient(proxy=self.proxy, follow_redirects=True) as client:
                r = await client.post(
                    self._DDG_LITE_URL,
                    data=data,
                    headers=headers,
                    timeout=10.0,
                )
                r.raise_for_status()

            # Parse results from HTML
            results = self._parse_lite_html(r.text, n)
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}  ({len(results)} results)\n"]
            for i, item in enumerate(results, 1):
                title = item["title"]
                url = item["url"]
                desc = item.get("desc", "")
                domain = urlparse(url).netloc.replace("www.", "") if url else ""
                meta = f"  [{domain}]" if domain else ""
                lines.append(f"{i}. {title}{meta}")
                lines.append(f"   {url}")
                if desc:
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            logger.error("WebSearch DDG error: {}", e)
            return f"Error: {e}"

    @staticmethod
    def _parse_lite_html(html_text: str, max_results: int) -> list[dict]:
        """Parse DuckDuckGo Lite HTML response into structured results."""
        results: list[dict] = []

        # Extract result links: <a rel="nofollow" href="URL" class="result-link">Title</a>
        link_pattern = re.compile(
            r'<a[^>]+rel="nofollow"[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a>',
            re.I,
        )
        # Extract snippets: <td class="result-snippet">...</td>
        snippet_pattern = re.compile(
            r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>',
            re.I | re.DOTALL,
        )

        links = link_pattern.findall(html_text)
        snippets = snippet_pattern.findall(html_text)

        for i, (url, title) in enumerate(links):
            if i >= max_results:
                break
            desc = ""
            if i < len(snippets):
                desc = _normalize(_strip_tags(snippets[i]))
            results.append({
                "title": html.unescape(title).strip(),
                "url": url,
                "desc": desc,
            })
        return results


class WebFetchTool(Tool):
    """Fetch and extract content from a URL.

    Uses Jina Reader API (r.jina.ai) for token-optimized markdown extraction.
    Falls back to python-readability if Jina is unavailable.
    """

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → clean markdown). Optimized for LLM token efficiency."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "maxChars": {"type": "integer", "minimum": 100, "description": "Max characters to return (default 20000)"}
        },
        "required": ["url"]
    }

    # Jina Reader endpoint
    _JINA_BASE = "https://r.jina.ai/"

    def __init__(self, max_chars: int = 20000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy

    async def execute(self, url: str, maxChars: int | None = None, **kwargs: Any) -> str:
        max_chars = maxChars or self.max_chars
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        # Try Jina Reader first (token-optimized markdown)
        text = await self._fetch_via_jina(url, max_chars)
        if text is not None:
            return json.dumps({"url": url, "text": text}, ensure_ascii=False)

        # Fallback to direct fetch + readability
        logger.info("Jina Reader failed for {}, falling back to readability", url)
        return await self._fetch_via_readability(url, max_chars)

    async def _fetch_via_jina(self, url: str, max_chars: int) -> str | None:
        """Fetch via Jina Reader API for clean, token-efficient markdown."""
        try:
            jina_url = f"{self._JINA_BASE}{url}"
            headers = {
                "X-Return-Format": "markdown",
                "X-No-Cache": "true",
                "Accept": "text/plain",
            }
            async with httpx.AsyncClient(
                timeout=30.0,
                proxy=self.proxy,
            ) as client:
                r = await client.get(jina_url, headers=headers)
                r.raise_for_status()

            text = r.text.strip()
            if not text or len(text) < 50:
                logger.warning("Jina Reader returned too short content for {}: {} chars", url, len(text))
                return None

            # Jina returns HTTP 200 with error text for blocked/failed urls
            if text.startswith("Warning:") or "returned error" in text[:200]:
                logger.info("Jina Reader got upstream error for {}: {}", url, text[:120])
                return None

            if len(text) > max_chars:
                text = text[:max_chars]
            return text
        except Exception as e:
            logger.warning("Jina Reader error for {}: {}", url, e)
            return None

    async def _fetch_via_readability(self, url: str, max_chars: int) -> str:
        """Fallback: fetch directly and extract with python-readability."""
        from readability import Document

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                timeout=30.0,
                proxy=self.proxy,
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            if "application/json" in ctype:
                text = json.dumps(r.json(), indent=2, ensure_ascii=False)
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = self._to_markdown(doc.summary())
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
            else:
                text = r.text

            if len(text) > max_chars:
                text = text[:max_chars]

            return json.dumps({"url": url, "text": text}, ensure_ascii=False)
        except Exception as e:
            logger.error("WebFetch readability error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    def _to_markdown(self, html_content: str) -> str:
        """Convert HTML to markdown (used by readability fallback)."""
        text = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                      lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html_content, flags=re.I)
        text = re.sub(r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
                      lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I)
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
