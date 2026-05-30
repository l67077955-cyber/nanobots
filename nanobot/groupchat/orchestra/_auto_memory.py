"""Post-discussion automatic memory extraction.

Called after broadcast_round returns. Runs 3 sequential LLM polls:
  1. Context + tools/methods that worked  → wing_code
  2. User preferences from instructions   → wing_user
  3. Extra fixed facts/discoveries        → wing_agent

Each poll extracts structured content from the conversation and stores
it via tool_add_drawer (direct ChromaDB call, no LLM tool loop needed).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    pass  # engine type is Any at runtime

# ── 3 extraction prompts ──────────────────────────────────────

_POLL_1_CONTEXT_TOOLS = """你是一个记忆提取助手。从以下群聊记录中提取**技术上下文和工具方法**。

提取规则：
- 这次讨论用了什么工具/方法跑通的？（具体命令、API、代码片段）
- 关键参数是什么？（如API端点、配置项、版本号、路径等）
- 完整的调用链是什么？（从输入到输出的完整流程）
- 遇到了什么坑/错误，怎么解决的？
- 有什么失败的尝试？（错误信息、失败原因、为什么失败）
- 关键的技术结论是什么？

输出格式（纯文本，不要 JSON/markdown 标题）：
## 工具与方法
（具体工具名、命令、参数）

## 关键参数
（API端点、配置项、版本号、路径等）

## 调用链
（从输入到输出的完整流程）

## 踩坑与解决
（错误描述 + 解决方案）

## 失败尝试
（错误信息、失败原因、为什么失败）

## 技术结论
（确认的事实、路径、配置）

如果某项无内容则写"无"。控制在 1200 字以内。

---

群聊记录：
{conversation}
"""

_POLL_2_USER_PREFS = """你是一个用户偏好提取助手。从以下群聊记录中提取**用户偏好和习惯**。

提取规则：
- 用户说了什么暗示了他的偏好？（如"别"/"快一点"/"发我"等指令风格）
- 用户对输出格式有什么要求？
- 用户对工具/技术栈有什么倾向？
- 用户的交互风格是什么？（极简/详细/结果导向等）

输出格式（纯文本）：
## 指令风格
（用户常用的简短指令及其含义）

## 输出偏好
（格式、长度、语言等偏好）

## 技术倾向
（偏好的工具/技术/平台）

## 交互风格
（整体沟通风格描述）

如果某项无内容则写"无"。控制在 500 字以内。

---

群聊记录：
{conversation}
"""

_POLL_3_FIXED_INFO = """你是一个信息提取助手。从以下群聊记录中提取**值得长期记忆的固定信息**。

提取规则：
- 有什么事实/数据/配置值得下次复用？
- 有什么发现/洞察是跨任务通用的？
- 有什么规则/约定被确认或更新了？
- 有什么失败的尝试值得记录？（避免重复踩坑）

输出格式（纯文本）：
## 事实与数据
（具体数值、路径、配置、版本等）

## 发现与洞察
（跨任务通用的发现）

## 规则与约定
（确认或更新的规则）

## 失败经验
（值得记录的失败尝试，避免重复踩坑）

如果某项无内容则写"无"。控制在 600 字以内。

---

群聊记录：
{conversation}
"""

# ── Room naming helpers ───────────────────────────────────────

import re

def _slugify(text: str, max_len: int = 40) -> str:
    """Create a URL-safe slug from text."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text[:max_len].strip('-') or "untitled"


def _build_conversation_text(history: list[dict], max_chars: int = 20000) -> str:
    """Format conversation history into text for LLM extraction."""
    lines = []
    total = 0
    for m in history:
        sender = m.get("sender", "?")
        content = m.get("content", "")
        if not content:
            continue
        # Truncate long messages
        if len(content) > 1500:
            content = content[:1500] + "…"
        line = f"[{sender}] {content}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _extract_topic_slug(topic: str) -> str:
    """Extract a short slug from the topic for room naming."""
    if not topic:
        return "general"
    # Take first meaningful phrase
    topic = topic.strip().lstrip("话题：").lstrip("话题:")
    return _slugify(topic, max_len=30) or "general"


# ── Core extraction logic ────────────────────────────────────

async def auto_store_memories(
    engine: Any,
    history: list[dict],
    topic: str = "",
) -> dict:
    """Run 3 sequential LLM polls to extract and store memories.

    Returns stats dict with extraction results.
    """
    from nanobot.groupchat.history.history_settings import (
        summarize_model as _get_summarize_model,
    )

    stats = {"polls": [], "stored": 0, "skipped": 0, "errors": []}

    # Get provider
    provider = getattr(engine, "provider", None)
    if not provider:
        logger.warning("auto_store_memories: no provider, skipping")
        stats["errors"].append("no_provider")
        return stats

    model = _get_summarize_model()
    conversation = _build_conversation_text(history)

    if len(conversation) < 100:
        logger.info("auto_store_memories: conversation too short, skipping")
        stats["skipped"] = 3
        return stats

    topic_slug = _extract_topic_slug(topic)
    timestamp = datetime.now().strftime("%Y%m%d")

    # ── Poll 1: Context + Tools → wing_code ──
    try:
        result1 = await provider.chat_with_retry(
            messages=[{"role": "user", "content": _POLL_1_CONTEXT_TOOLS.format(conversation=conversation)}],
            model=model,
            max_tokens=1800,
            temperature=0.3,
        )
        content1 = (result1.content or "").strip()
        if content1 and len(content1) > 50 and "无" not in content1[:20]:
            _store_drawer(
                wing="wing_code",
                room=f"round-{timestamp}-{topic_slug}",
                content=content1,
                source="auto_memory_poll_1",
            )
            stats["stored"] += 1
            stats["polls"].append({"poll": 1, "wing": "wing_code", "chars": len(content1)})
            logger.info("auto_store_memories: poll 1 stored (wing_code, {} chars)", len(content1))
        else:
            stats["skipped"] += 1
            stats["polls"].append({"poll": 1, "reason": "empty_or_trivial"})
    except Exception as e:
        logger.error("auto_store_memories: poll 1 failed: {}", e)
        stats["errors"].append(f"poll_1: {e}")

    # ── Poll 2: User Preferences → wing_user ──
    try:
        result2 = await provider.chat_with_retry(
            messages=[{"role": "user", "content": _POLL_2_USER_PREFS.format(conversation=conversation)}],
            model=model,
            max_tokens=800,
            temperature=0.3,
        )
        content2 = (result2.content or "").strip()
        if content2 and len(content2) > 50 and "无" not in content2[:20]:
            _store_drawer(
                wing="wing_user",
                room=f"preferences-{timestamp}",
                content=content2,
                source="auto_memory_poll_2",
            )
            stats["stored"] += 1
            stats["polls"].append({"poll": 2, "wing": "wing_user", "chars": len(content2)})
            logger.info("auto_store_memories: poll 2 stored (wing_user, {} chars)", len(content2))
        else:
            stats["skipped"] += 1
            stats["polls"].append({"poll": 2, "reason": "empty_or_trivial"})
    except Exception as e:
        logger.error("auto_store_memories: poll 2 failed: {}", e)
        stats["errors"].append(f"poll_2: {e}")

    # ── Poll 3: Fixed Info → wing_agent ──
    try:
        result3 = await provider.chat_with_retry(
            messages=[{"role": "user", "content": _POLL_3_FIXED_INFO.format(conversation=conversation)}],
            model=model,
            max_tokens=800,
            temperature=0.3,
        )
        content3 = (result3.content or "").strip()
        if content3 and len(content3) > 50 and "无" not in content3[:20]:
            _store_drawer(
                wing="wing_agent",
                room=f"round-{timestamp}-{topic_slug}",
                content=content3,
                source="auto_memory_poll_3",
            )
            stats["stored"] += 1
            stats["polls"].append({"poll": 3, "wing": "wing_agent", "chars": len(content3)})
            logger.info("auto_store_memories: poll 3 stored (wing_agent, {} chars)", len(content3))
        else:
            stats["skipped"] += 1
            stats["polls"].append({"poll": 3, "reason": "empty_or_trivial"})
    except Exception as e:
        logger.error("auto_store_memories: poll 3 failed: {}", e)
        stats["errors"].append(f"poll_3: {e}")

    logger.info(
        "auto_store_memories: done — stored={}, skipped={}, errors={}",
        stats["stored"], stats["skipped"], len(stats["errors"]),
    )
    return stats


# ── Self-referential filter ────────────────────────────────────

_SELF_REFERENTIAL_KEYWORDS = [
    "memory_palace", "auto_memory", "记忆检索", "记忆存储",
    "记忆宫殿", "记忆提取", "auto_recall", "auto_store",
    "语义搜索", "关键词生成", "_generate_wing_queries",
]


def _is_self_referential(content: str) -> bool:
    """Check if content is about the memory system itself (meta-discussion)."""
    hits = sum(1 for kw in _SELF_REFERENTIAL_KEYWORDS if kw in content)
    return hits >= 2


# ── Deduplication via similarity check ────────────────────────

def _is_duplicate(content: str, wing: str, threshold: float = 0.60) -> bool:
    """Check if similar content already exists in the given wing."""
    try:
        from mempalace.mcp_server import tool_search
        result = tool_search(query=content[:200], wing=wing, limit=3)
        for r in result.get("results", []):
            if r.get("similarity", 0) >= threshold:
                logger.info(
                    "auto_store: dedup skipped (sim={:.2f}) in {}",
                    r["similarity"], wing,
                )
                return True
    except Exception as e:
        logger.warning("auto_store: dedup check failed: {}", e)
    return False


def _store_drawer(wing: str, room: str, content: str, source: str = "auto_memory") -> dict | None:
    """Direct ChromaDB store via tool_add_drawer (no LLM tool loop).
    
    Returns None if content is self-referential or duplicate.
    """
    # Guard 1: self-referential filter
    if _is_self_referential(content):
        logger.info("auto_store: self-referential content skipped in {}", wing)
        return None

    # Guard 2: deduplication
    if _is_duplicate(content, wing):
        return None

    from mempalace.mcp_server import tool_add_drawer
    return tool_add_drawer(
        wing=wing,
        room=room,
        content=content,
        source_file=source,
        added_by="auto_memory",
    )
