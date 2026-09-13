"""Result store — archive oversized tool results, keep an index inline.

Context-engineering primitive (plan.md 2026-09-13 backlog #1): a 20-50KB
web_fetch/tool output pasted into the shared history inflates every agent's
view (observed 216K chars → thinking models degrade to text-protocol output
and prefix-cache hit-rates collapse to 3-33%). Instead:

- tool results above ``inline_threshold_chars`` are archived to a
  content-addressed store (memory + append-only jsonl on disk);
- the message keeps a one-line index stub:
  ``[已归档 tr-xxxxxxxx | 原文 N 字符 | get_tool_result(index=...) 可取回]``
- agents fetch the original on demand via the ``get_tool_result`` tool
  (supports offset/limit paging for huge payloads).

Storage is process-global and survives restarts (jsonl replay on boot).
Dedup: identical content → identical index (sha1 prefix).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.tools.base import Tool

_INDEX_PREFIX = "tr-"
_DEFAULT_THRESHOLD = 6_000
_DEFAULT_KEEP_RECENT = 3
_DEFAULT_PAGE = 4_000
_STUB_TEMPLATE = (
    "[已归档 {index} | 原文 {size:,} 字符 | "
    "调用 get_tool_result(index=\"{index}\", offset=0, limit=4000) 按需取回原文]"
)


class ResultStore:
    """Content-addressed archive for oversized tool results."""

    def __init__(self, store_dir: Path | None = None) -> None:
        self._dir = store_dir or (Path.home() / ".nanobot" / "tool_results")
        self._file = self._dir / "store.jsonl"
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._loaded = False

    # ── boot / persistence ────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                if self._file.exists():
                    for line in self._file.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            idx = rec.get("index")
                            content = rec.get("content")
                            if isinstance(idx, str) and isinstance(content, str):
                                self._cache[idx] = content
                        except json.JSONDecodeError:
                            continue
            except Exception as e:  # noqa: BLE001
                logger.warning("ResultStore: load failed (starting empty): {}", e)
            self._loaded = True

    def _append_disk(self, index: str, content: str) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"index": index, "content": content}, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.debug("ResultStore: disk append failed (memory-only): {}", e)

    # ── API ───────────────────────────────────────────────────────────────

    def put(self, content: str) -> str:
        """Archive content, return its stable index (content-addressed)."""
        self._ensure_loaded()
        digest = hashlib.sha1(content.encode("utf-8", errors="replace")).hexdigest()[:10]
        index = f"{_INDEX_PREFIX}{digest}"
        with self._lock:
            if index not in self._cache:
                self._cache[index] = content
                self._append_disk(index, content)
        return index

    def has(self, index: str) -> bool:
        self._ensure_loaded()
        with self._lock:
            return index in self._cache

    def get(self, index: str, offset: int = 0, limit: int | None = None) -> str:
        """Fetch (a page of) archived content by index."""
        self._ensure_loaded()
        with self._lock:
            content = self._cache.get(index)
        if content is None:
            return (
                f"Error: index '{index}' not found. It may reference a result "
                f"archived before the store existed or already evicted."
            )
        total = len(content)
        limit = _DEFAULT_PAGE if limit is None else max(1, min(int(limit), 50_000))
        offset = max(0, int(offset))
        chunk = content[offset:offset + limit]
        more = f" | 还有 {total - offset - len(chunk):,} 字符，继续用 offset={offset + len(chunk)} 翻页" if offset + len(chunk) < total else ""
        header = f"[{index} 共 {total:,} 字符 本页 {len(chunk):,}]" if total > limit else ""
        return (header + "\n" if header else "") + chunk + (more if more else "")

    def stub_for(self, index: str, original_size: int) -> str:
        return _STUB_TEMPLATE.format(index=index, size=original_size)

    def size(self) -> int:
        self._ensure_loaded()
        with self._lock:
            return len(self._cache)


_store: ResultStore | None = None


def get_result_store() -> ResultStore:
    """Process-global store singleton."""
    global _store
    if _store is None:
        _store = ResultStore()
    return _store


# ── Message-side compression ──────────────────────────────────────────────

def _settings() -> tuple[bool, int, int]:
    try:
        from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
            result_store_enabled,
            result_store_inline_threshold,
            result_store_keep_recent,
        )
        return (
            result_store_enabled(),
            result_store_inline_threshold(),
            result_store_keep_recent(),
        )
    except Exception:
        return True, _DEFAULT_THRESHOLD, _DEFAULT_KEEP_RECENT


def compress_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int | None = None,
    threshold: int | None = None,
) -> list[dict[str, Any]]:
    """Replace oversized tool-result contents with archive stubs.

    Scans messages oldest-first; the newest ``keep_recent`` tool messages are
    always left verbatim (the agent is actively working with them). Returns
    the original list when nothing changes (copy-on-write otherwise).
    """
    enabled, cfg_threshold, cfg_keep = _settings()
    if not enabled:
        return messages
    threshold = cfg_threshold if threshold is None else threshold
    keep_recent = cfg_keep if keep_recent is None else keep_recent

    tool_positions = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool" and isinstance(m.get("content"), str)
    ]
    archivable = tool_positions[:-keep_recent] if keep_recent > 0 else tool_positions
    to_archive = [
        i for i in archivable
        if len(messages[i]["content"]) > threshold and _INDEX_PREFIX not in messages[i]["content"][:40]
    ]
    if not to_archive:
        return messages

    store = get_result_store()
    out = list(messages)
    for i in to_archive:
        original = out[i]["content"]
        index = store.put(original)
        out[i] = {**out[i], "content": store.stub_for(index, len(original))}
    logger.debug(
        "ResultStore: archived {} tool result(s), {} bytes → stubs",
        len(to_archive),
        sum(len(messages[i]["content"]) for i in to_archive),
    )
    return out


class GetToolResultTool(Tool):
    """Fetch an archived tool result by its index (with paging)."""

    @property
    def name(self) -> str:
        return "get_tool_result"

    @property
    def description(self) -> str:
        return (
            "按索引取回被归档的大体积工具结果原文。历史中超大工具输出会被替换为"
            " '[已归档 tr-xxxx ...]' 索引行；需要查看原文细节时用本工具。"
            "支持 offset/limit 分页浏览超长内容。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "index": {"type": "string", "description": "归档索引，如 tr-1a2b3c4d5e"},
                "offset": {"type": "integer", "description": "起始字符偏移（默认 0）"},
                "limit": {"type": "integer", "description": "本页字符数（默认 4000，最大 50000）"},
            },
            "required": ["index"],
        }

    async def execute(self, index: str = "", offset: int = 0, limit: int | None = None, **_: Any) -> str:
        if not index or not isinstance(index, str):
            return "Error: index is required (e.g. 'tr-1a2b3c4d5e')"
        return get_result_store().get(index.strip(), offset=offset, limit=limit)
