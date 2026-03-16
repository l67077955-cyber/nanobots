"""Async group chat engine for multi-agent discussions.

Supports fluid agent management:
- Agent registry: all available agents (loaded from config/directory)
- Active participants: agents currently in the conversation
- Seamlessly transitions between 1-on-1 and group chat as agents are added/removed
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timezone, timedelta

_CST = timezone(timedelta(hours=8))

def _cn_now() -> datetime:
    """Return current time in China Standard Time (UTC+8)."""
    return datetime.now(_CST)
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.agents import load_agents
from nanobot.groupchat.config import GroupChatConfig
from nanobot.providers.base import LLMProvider


class GroupChatEngine:
    """Async multi-agent group chat engine with fluid agent management.

    Usage:
        engine = GroupChatEngine(config, provider, workspace)

        # Registry is pre-loaded; active participants start empty
        engine.add_agent("Benjamin")    # 1 agent → direct replies
        engine.add_agent("Grok")        # 2+ agents → group chat auto-starts
        engine.remove_agent("Benjamin") # back to 1 → group loop stops
        engine.remove_agent("Grok")     # 0 agents → silent
    """

    # All available tool names for granular control
    TOOL_NAMES = [
        "web_search", "web_fetch", "exec",
        "read_file", "write_file", "edit_file", "list_dir",
    ]

    def __init__(
        self,
        config: GroupChatConfig,
        provider: LLMProvider,
        workspace: Path,
        brave_api_key: str = "",
    ):
        self.config = config
        self.provider = provider
        self.workspace = workspace
        self.brave_api_key = brave_api_key
        self._pm_cache: dict | None = None
        self._register_tools()

    def _register_tools(self) -> None:
        # Tool registry (same tools as core AgentLoop)
        # Lazy import to avoid circular: engine → agent.tools → agent → config → groupchat
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
        from nanobot.agent.tools.shell import ExecTool
        from nanobot.agent.tools.filesystem import (
            ReadFileTool, WriteFileTool, EditFileTool, ListDirTool,
        )

        home = str(Path.home())
        sandbox = Path("/tmp")

        # Web + file + exec tools (available to agents with tools_enabled)
        self.tools = ToolRegistry()
        self.tools.register(WebSearchTool(api_key=self.brave_api_key or None))
        self.tools.register(WebFetchTool())
        self.tools.register(ExecTool(timeout=120, working_dir=home))
        self.tools.register(ReadFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.tools.register(WriteFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.tools.register(EditFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.tools.register(ListDirTool(workspace=sandbox, allowed_dir=sandbox))

        # Direct chat tools (default agent gets same + full exec)
        self.direct_tools = ToolRegistry()
        self.direct_tools.register(ExecTool(timeout=120, working_dir=home))
        self.direct_tools.register(ReadFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(WriteFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(EditFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(ListDirTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(WebSearchTool(api_key=self.brave_api_key or None))
        self.direct_tools.register(WebFetchTool())
        logger.info("Groupchat: registered {} group tools, {} direct tools",
                    len(self.tools), len(self.direct_tools))

        # Registry: all known agents {name: {model, prompt}}
        self.registry: dict[str, dict[str, Any]] = load_agents(self.config, self.workspace)

        # Active participants in current conversation
        self._active_agents: list[str] = self._load_active()

        # Leader mode
        self._leader: str | None = self._load_leader()
        self._round: int = 0  # Current round number

        # Runtime state
        self._task: asyncio.Task | None = None
        self._running = False
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._send_fn: Callable[[str], Awaitable[None]] | None = None
        self._topic: str = ""
        self._history: list[dict[str, str]] = []
        self._request_log: list[dict[str, Any]] = []  # LLM request log
        self._session_dir: Path | None = None

    @property
    def is_running(self) -> bool:
        """True when group chat loop is actively running (2+ agents)."""
        return self._running

    @property
    def active_agents(self) -> list[str]:
        return list(self._active_agents)

    @property
    def available_agents(self) -> list[str]:
        return list(self.registry.keys())

    def set_send_fn(self, send_fn: Callable[[str], Awaitable[None]]) -> None:
        """Set the message output callback."""
        self._send_fn = send_fn

    # ── Agent Management ─────────────────────────────────────

    def add_agent(self, name: str) -> str:
        """Add an agent to the active conversation.

        Returns status message.
        """
        # Case-insensitive lookup
        matched = self._resolve_agent_name(name)
        if not matched:
            return f"❌ Agent '{name}' 不存在。可用: {', '.join(self.registry.keys())}"

        if matched in self._active_agents:
            return f"⚠️ {matched} 已在对话中"

        self._active_agents.append(matched)
        self._save_active()
        logger.info("Groupchat: added agent {}, active={}", matched, self._active_agents)

        # Don't auto-start loop here — inject() will lazy-start
        # when user sends the first message with 2+ agents.
        if len(self._active_agents) >= 2:
            return (
                f"✅ {matched} 加入对话！\n"
                f"👥 当前成员: {', '.join(self._active_agents)}\n"
                f"📌 直接发消息开始群聊"
            )

        return f"✅ {matched} 加入对话\n👥 当前成员: {', '.join(self._active_agents)}"

    def remove_agent(self, name: str) -> str:
        """Remove an agent from the active conversation.

        Returns status message.
        """
        matched = self._resolve_agent_name(name)
        if not matched or matched not in self._active_agents:
            return f"⚠️ {name} 不在当前对话中。当前: {', '.join(self._active_agents) or '无'}"

        self._active_agents.remove(matched)
        self._save_active()
        logger.info("Groupchat: removed agent {}, active={}", matched, self._active_agents)

        # If below 2 agents, stop group loop
        if len(self._active_agents) < 2 and self._running:
            self._stop_group_loop()
            if self._active_agents:
                return (
                    f"✅ {matched} 已离开\n"
                    f"💬 回到与 {self._active_agents[0]} 的对话模式"
                )
            return f"✅ {matched} 已离开，无活跃 agent"

        return f"✅ {matched} 已离开\n👥 当前成员: {', '.join(self._active_agents)}"
    # ── Agent ordering ────────────────────────────────────

    def reorder_agents(self, new_order: list[str]) -> str:
        """Reorder active agents. new_order should contain all active agent names."""
        # Validate all names exist in active list
        resolved = []
        for name in new_order:
            matched = self._resolve_agent_name(name)
            if not matched or matched not in self._active_agents:
                return f"⚠️ {name} 不在活跃列表中"
            if matched in resolved:
                return f"⚠️ {matched} 重复了"
            resolved.append(matched)

        if set(resolved) != set(self._active_agents):
            missing = set(self._active_agents) - set(resolved)
            return f"⚠️ 缺少: {', '.join(missing)}"

        self._active_agents[:] = resolved
        self._save_active()
        order_str = " → ".join(resolved)
        return f"✅ 发言顺序已调整\n📢 {order_str}"

    # ── Leader Mode ─────────────────────────────────────────

    @property
    def leader(self) -> str | None:
        return self._leader

    def set_leader(self, name: str | None) -> str:
        """Set or clear the leader agent."""
        if name is None:
            old = self._leader
            self._leader = None
            self._save_leader()
            return f"✅ 已取消 Leader 模式" + (f" ({old})" if old else "")

        matched = self._resolve_agent_name(name)
        if not matched:
            return f"❌ Agent '{name}' 不存在。可用: {', '.join(self.registry.keys())}"

        self._leader = matched
        self._save_leader()
        return f"👑 {matched} 已设为 Leader\n其他 agent 会先发言，{matched} 最后汇总"

    def _save_leader(self) -> None:
        try:
            p = Path.home() / ".nanobot" / "leader.txt"
            if self._leader:
                p.write_text(self._leader)
            elif p.exists():
                p.unlink()
        except Exception:
            pass

    def _load_leader(self) -> str | None:
        p = Path.home() / ".nanobot" / "leader.txt"
        if p.exists():
            try:
                name = p.read_text().strip()
                if name and name in self.registry:
                    logger.info("Restored leader: {}", name)
                    return name
            except Exception:
                pass
        return None

    # ── Persistence ─────────────────────────────────────────

    @property
    def _active_file(self) -> Path:
        return Path.home() / ".nanobot" / "active_agents.json"

    def _save_active(self) -> None:
        """Persist active agents list (and order) to disk."""
        try:
            self._active_file.parent.mkdir(parents=True, exist_ok=True)
            self._active_file.write_text(json.dumps(self._active_agents, ensure_ascii=False))
        except Exception:
            pass

    def _load_active(self) -> list[str]:
        """Load active agents from disk, filtering out unregistered ones."""
        if self._active_file.exists():
            try:
                saved = json.loads(self._active_file.read_text())
                # Only keep agents that exist in registry
                valid = [a for a in saved if a in self.registry]
                if valid:
                    logger.info("Restored active agents: {}", valid)
                return valid
            except Exception:
                pass
        return []

    @property
    def _groups_file(self) -> Path:
        return Path.home() / ".nanobot" / "groups.json"

    def _load_groups(self) -> dict[str, list[str]]:
        if self._groups_file.exists():
            try:
                return json.loads(self._groups_file.read_text())
            except Exception:
                return {}
        return {}

    def _save_groups(self, groups: dict[str, list[str]]) -> None:
        self._groups_file.parent.mkdir(parents=True, exist_ok=True)
        self._groups_file.write_text(json.dumps(groups, ensure_ascii=False, indent=2))

    def save_group(self, name: str) -> str:
        """Save current active agents as a named group."""
        if not self._active_agents:
            return "⚠️ 没有活跃 agent，无法保存"
        groups = self._load_groups()
        groups[name] = list(self._active_agents)
        self._save_groups(groups)
        return f"✅ 已保存分组 「{name}」\n👥 成员: {', '.join(self._active_agents)}"

    def load_group(self, name: str) -> str:
        """Load a saved group config, setting agents directly."""
        groups = self._load_groups()
        if name not in groups:
            available = ', '.join(groups.keys()) if groups else '无'
            return f"⚠️ 分组 「{name}」 不存在\n📋 可用分组: {available}"

        target = groups[name]

        # Stop any running loop first
        self._stop_group_loop()

        # Set agents directly (no add/remove to avoid loop race)
        valid = [a for a in target if self._resolve_agent_name(a)]
        self._active_agents = valid
        self._save_active()

        # Start group loop if 2+ agents
        if len(self._active_agents) >= 2:
            self._start_group_loop()

        return (
            f"✅ 已载入分组 「{name}」\n"
            f"👥 当前成员: {', '.join(self._active_agents)}"
        )

    def delete_group(self, name: str) -> str:
        """Delete a saved group config."""
        groups = self._load_groups()
        if name not in groups:
            return f"⚠️ 分组 「{name}」 不存在"
        del groups[name]
        self._save_groups(groups)
        return f"🗑 已删除分组 「{name}」"

    def list_groups(self) -> str:
        """List all saved group configs."""
        groups = self._load_groups()
        if not groups:
            return "📋 没有保存的分组\n用 /savegroup <名称> 保存当前成员"
        lines = ["📋 已保存的分组："]
        for gname, members in groups.items():
            lines.append(f"  • {gname}: {', '.join(members)}")
        return "\n".join(lines)

    # ── Response cleanup ───

    def _clean_response(self, content: str, agent_name: str) -> str:
        """Clean up model response: strip think blocks, name prefixes, etc."""
        import re

        # 1. Strip <think>...</think> blocks (deepseek, some models)
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()

        # 2. Strip Grok-style reasoning prefix (starts with "A: " or similar)
        #    Detect: block of reasoning lines before actual content
        if content.startswith("A: ") or content.startswith("A："):
            # Find where reasoning ends and actual content starts
            lines = content.split("\n")
            # Find where the real content begins (after an empty line or marker)
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped and not stripped.startswith("A:") and not stripped.startswith("A："):
                    # Check if previous line was empty (paragraph break)
                    if i > 0 and not lines[i-1].strip():
                        content = "\n".join(lines[i:])
                        break

        # 3. Strip any agent name prefix from the start
        #    Handles: "Benjamin: reply" or "Harper: reply"
        all_names = list(self.registry.keys())
        for name in all_names:
            for sep in (": ", "：", ":\n"):
                prefix = f"{name}{sep}"
                if content.startswith(prefix):
                    content = content[len(prefix):]
                    break

        return content.strip()

    # ── Tool-augmented chat (matching AgentLoop._run_agent_loop) ───

    # Search intent keywords — only pass tools when user message matches
    _SEARCH_KEYWORDS = {
        # Chinese
        "搜索", "搜一下", "查一下", "查找", "查询", "找一下",
        "新闻", "最新", "今天", "今日", "实时", "热点",
        "帮我查", "帮我搜", "帮我找", "上网",
        # English
        "search", "look up", "find", "google", "news",
        "latest", "recent", "today", "current",
    }

    @staticmethod
    def _has_search_intent(messages: list[dict[str, Any]]) -> bool:
        """Check if the latest user message has search intent."""
        # Find the last user message
        for msg in reversed(messages):
            if msg.get("role") == "user":
                text = msg.get("content", "").lower()
                for kw in GroupChatEngine._SEARCH_KEYWORDS:
                    if kw in text:
                        return True
                return False
        return False

    def _get_agent_tools(self, agent_cfg: dict, registry) -> list:
        """Get filtered tool definitions based on agent's per-tool config.

        Config format:
          tools: {web_search: true, exec: false, ...}  # granular
          tools_enabled: true  # legacy: all tools on
        """
        # Granular tools dict
        tools_cfg = agent_cfg.get("tools")
        if isinstance(tools_cfg, dict):
            enabled = {k for k, v in tools_cfg.items() if v}
            if not enabled:
                return []
            return [d for d in registry.get_definitions()
                    if d.get("function", {}).get("name") in enabled]

        # Legacy fallback
        if agent_cfg.get("tools_enabled", False):
            return registry.get_definitions()

        # Default agent (_default flag)
        if agent_cfg.get("_default"):
            return registry.get_definitions()

        return []

    async def _chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        model: str,
        agent_name: str,
        max_iterations: int = 5,
        is_direct: bool = False,
    ) -> tuple[str, list[str], dict[str, Any]]:
        """Chat with tool calling loop, matching nanobot core agent logic.

        Returns (content, tools_used, stats).
        """
        # Tool selection:
        # - Direct chat (1-on-1): always provide exec + web tools
        # - Group chat: only web tools, only when user has search intent
        agent_cfg = self.registry.get(agent_name, {})
        tool_defs: list = []
        if is_direct:
            tool_defs = self._get_agent_tools(agent_cfg, self.direct_tools)
        else:
            tool_defs = self._get_agent_tools(agent_cfg, self.tools)
        tools_used: list[str] = []
        tool_calls_detail: list[dict] = []  # name, args, result_preview
        iteration = 0
        import time as _time
        total_tokens = {"prompt": 0, "completion": 0, "total": 0}
        total_latency = 0.0
        call_details: list[dict] = []  # Per-iteration records

        while iteration < max_iterations:
            iteration += 1
            # Langfuse trace metadata for this agent call
            trace_metadata = {
                "trace_name": f"{'direct' if is_direct else 'group'}_{agent_name}",
                "trace_user_id": "groupchat",
                "tags": [agent_name, "direct" if is_direct else "group"],
                "generation_name": f"{agent_name}_iter{iteration}",
            }


            t0 = _time.time()
            response = await self.provider.chat(
                messages=messages,
                tools=tool_defs if tool_defs else None,
                model=model,
                max_tokens=self.config.max_tokens,
                metadata=trace_metadata,
            )
            latency = _time.time() - t0
            total_latency += latency

            # Accumulate token usage
            usage = response.usage or {}
            total_tokens["prompt"] += usage.get("prompt_tokens", 0)
            total_tokens["completion"] += usage.get("completion_tokens", 0)
            total_tokens["total"] += usage.get("total_tokens", 0)

            call_details.append({
                "iter": iteration,
                "latency": round(latency, 2),
                "tokens": dict(usage),
                "finish": response.finish_reason,
                "tools": [tc.name for tc in response.tool_calls] if response.has_tool_calls else [],
            })

            raw_content = (response.content or "")[:100]
            logger.info("Agent {} iter {}: finish={} tools={} content='{}'",
                        agent_name, iteration, response.finish_reason,
                        bool(response.has_tool_calls), raw_content)

            if response.has_tool_calls:
                # Show tool usage hint (simplified)
                for tc in response.tool_calls:
                    # Show just the key argument value
                    args = tc.arguments or {}
                    short = args.get("command") or args.get("query") or args.get("url") or args.get("path") or ""
                    if not short and args:
                        short = list(args.values())[0]
                    if isinstance(short, str) and len(short) > 80:
                        short = short[:80] + "…"
                    logger.info("Agent {} calling tools: {}", agent_name, tc.name)
                    if self._send_fn:
                        await self._send(f"🔧 {agent_name} → `{tc.name}`: {short}")

                # Append assistant message with tool_calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": tool_call_dicts,
                })

                # Execute each tool and append results
                tool_registry = self.direct_tools if is_direct else self.tools
                for tc in response.tool_calls:
                    tools_used.append(tc.name)
                    logger.info("Executing tool: {}({})", tc.name,
                                json.dumps(tc.arguments, ensure_ascii=False)[:200])
                    result = await tool_registry.execute(tc.name, tc.arguments)
                    # Record tool call detail
                    tool_calls_detail.append({
                        "name": tc.name,
                        "args": json.dumps(tc.arguments, ensure_ascii=False)[:200],
                        "result_len": len(result) if result else 0,
                        "result_preview": (result or "")[:150],
                    })
                    # Show brief result preview
                    if self._send_fn and result:
                        preview = result.strip().replace("\n", " ")[:100]
                        if len(result) > 100:
                            preview += "…"
                        await self._send(f"   ↳ {preview}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result[:2000],  # Truncate large results
                    })
            else:
                # Text response — done
                content = self._clean_response(response.content or "", agent_name)
                stats = {
                    "iterations": iteration,
                    "latency": round(total_latency, 2),
                    "tokens": total_tokens,
                    "calls": call_details,
                    "tool_calls_detail": tool_calls_detail,
                }
                return content, tools_used, stats

        # Max iterations reached
        logger.warning("Agent {} hit max tool iterations ({})", agent_name, max_iterations)
        content = self._clean_response(response.content or "", agent_name)
        stats = {
            "iterations": iteration,
            "latency": round(total_latency, 2),
            "tokens": total_tokens,
            "calls": call_details,
            "tool_calls_detail": tool_calls_detail,
        }
        return content, tools_used, stats

    async def direct_chat(self, user_message: str) -> str | None:
        """Send message to single active agent (1-on-1 mode).

        Uses proper multi-message format (like SillyTavern) with tool calling
        matching nanobot core agent logic.
        """
        if len(self._active_agents) != 1:
            return None

        agent_name = self._active_agents[0]
        agent = self.registry[agent_name]

        # Build messages matching SillyTavern's minimal flow:
        # system(persona) → [history as user/assistant] → user(new)
        now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": agent["prompt"] + f"\n\n[Current date and time: {now}]"},
        ]

        # Few-shot examples (only if EXAMPLES.md exists)
        examples = agent.get("examples", "")
        if examples:
            messages.append({"role": "system", "content": f"以下是对话风格示例：\n{examples}"})

        # Chat history as proper alternating messages
        messages.extend(self._history_to_messages(agent_name))

        # Current user message
        messages.append({"role": "user", "content": user_message})

        # Post-history instructions only if explicitly defined
        instructions = agent.get("instructions", "")
        if instructions:
            messages.append({"role": "system", "content": instructions})

        try:
            content, tools_used, stats = await self._chat_with_tools(
                messages=messages,
                model=agent["model"],
                agent_name=agent_name,
                is_direct=True,
            )
            self._request_log.append({
                "agent": agent_name, "model": agent["model"],
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": len(content), "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "direct", "tools": tools_used,
                "input_preview": user_message[:200],
                "output": content[:500],
                **stats,
            })
            if content:
                self._add_message("用户", user_message)
                self._add_message(agent_name, content)
                return f"💬 {agent_name}:\n\n{content}"
            else:
                return f"⚠️ {agent_name} 返回空回复 (模型可能暂时异常，请重试)"
        except Exception as e:
            logger.error("Direct chat with {} failed: {}", agent_name, e)
            self._request_log.append({
                "agent": agent_name, "model": agent["model"],
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": 0, "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "direct", "error": str(e),
            })
            return f"⚠️ {agent_name} 回复失败: {e}"

    def inject(self, message: str) -> None:
        """Inject a user message into the group chat."""
        # Lazy start: if 2+ agents and loop not running, start it now
        if not self._running and len(self._active_agents) >= 2:
            self._start_group_loop()
        if self._running:
            self._input_queue.put_nowait(message)

    def request_summary(self) -> None:
        """Request a discussion summary."""
        if self._running:
            self._input_queue.put_nowait("__SUMMARY__")

    def stop(self) -> None:
        """Stop everything and clear active agents."""
        self._stop_group_loop()
        self._active_agents.clear()
        self._save_active()

    # ── Internal ─────────────────────────────────────────────

    def _resolve_agent_name(self, name: str) -> str | None:
        """Case-insensitive agent name resolution."""
        for reg_name in self.registry:
            if reg_name.lower() == name.lower():
                return reg_name
        return None

    def _start_group_loop(self) -> None:
        """Start the async group chat loop."""
        # Always cancel any prior task to avoid duplicate loops
        if self._task and not self._task.done():
            self._task.cancel()
        if self._running:
            return
        self._running = True
        self._input_queue = asyncio.Queue()
        if not self._topic:
            self._topic = "自由讨论"

        # Session directory
        timestamp = _cn_now().strftime("%Y%m%d-%H%M%S")
        sessions_dir = Path.home() / ".nanobot" / "collab-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = sessions_dir / f"gc-{timestamp}"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._task = asyncio.create_task(self._run_loop())
        logger.info("Group chat loop started with {}", self._active_agents)

    def _stop_group_loop(self) -> None:
        """Stop the group chat loop (keeps active agents and history)."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _send(self, text: str) -> None:
        if self._send_fn:
            try:
                await self._send_fn(text)
            except Exception as e:
                logger.error("Groupchat send failed: {}", e)

    def _add_message(self, sender: str, content: str) -> None:
        self._history.append({"sender": sender, "content": content})
        if len(self._history) > self.config.max_history:
            self._history = self._history[-self.config.max_history:]
        if self._session_dir:
            with open(self._session_dir / "chat_log.txt", "a") as f:
                f.write(f"[{sender}]: {content}\n---\n")

    def _format_history(self) -> str:
        return "\n\n".join(f"[{m['sender']}]: {m['content']}" for m in self._history)

    def _history_to_messages(self, current_agent: str = "") -> list[dict[str, Any]]:
        """Convert history into proper API messages (SillyTavern style).

        Uses `name` field + content prefix for attribution,
        matching SillyTavern's DEFAULT character_names_behavior.
        """
        msgs: list[dict[str, Any]] = []
        for m in self._history:
            sender = m["sender"]
            content = m["content"]
            if sender == "用户":
                msgs.append({"role": "user", "content": content})
            elif sender == "系统":
                msgs.append({"role": "system", "content": content})
            else:
                # SillyTavern DEFAULT: name field + content prefix
                msgs.append({
                    "role": "assistant",
                    "content": f"{sender}: {content}",
                    "name": sender.replace(" ", "_"),
                })
        return msgs

    def _pick_next_speaker(self, last_content: str = "") -> str:
        names = self._active_agents
        # @mentions
        for name in names:
            if f"@{name}" in last_content or f"@{name.lower()}" in last_content:
                return name
        # Implicit mentions
        mentioned = [n for n in names if n.lower() in last_content.lower()
                     and (not self._history or self._history[-1]["sender"] != n)]
        if mentioned:
            return random.choice(mentioned)
        # Avoid repeat
        candidates = list(names)
        if self._history:
            last_speaker = self._history[-1]["sender"]
            candidates = [n for n in candidates if n != last_speaker]
        return random.choice(candidates) if candidates else random.choice(names)

    def _build_agent_prompt(self, agent_name: str) -> list[dict[str, Any]]:
        """Build prompt matching SillyTavern's group chat prompt structure.

        SillyTavern Chat Completion order:
        1. system: main_prompt — "Write {char}'s next reply..."
        2. system: new_group_chat — "[Group members: ...]"
        3. system: charDescription — persona/SOUL.md
        4. system: examples (optional)
        5. [chat history with name field]
        6. system: instructions (optional, post-history)
        7. system: group_nudge — ALWAYS last
        """
        agent = self.registry[agent_name]
        group_members = ", ".join(self._active_agents)

        messages: list[dict[str, Any]] = []

        # 1. Main prompt (SillyTavern: default_main_prompt)
        main_prompt = (
            f"Write {agent_name}'s next reply in a fictional group chat. "
            f"Write 1 reply only in character as {agent_name}. "
            f"Do not write as or for other characters."
        )
        messages.append({"role": "system", "content": main_prompt})

        # 2. Group context with current date (SillyTavern: new_group_chat_prompt)
        from datetime import datetime
        now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
        messages.append({"role": "system", "content": (
            f"[Start a new group chat. Group members: {group_members}]\n"
            f"[Current date and time: {now}]"
        )})

        # 3. Character description (SillyTavern: charDescription)
        messages.append({"role": "system", "content": agent["prompt"]})

        # 3.5 Agentic tool instructions (for tools_enabled agents)
        agent_cfg = self.registry.get(agent_name, {})
        if agent_cfg.get("tools_enabled", False) or agent_cfg.get("_default"):
            tool_prompt = (
                "[Tool Usage Instructions]\n"
                "You have access to these tools: exec (bash commands), "
                "read_file, write_file, edit_file, list_dir, web_search, web_fetch.\n\n"
                "Guidelines:\n"
                "- USE tools proactively. Don't say 'I can't' when you have tools.\n"
                "- For complex tasks: briefly state your plan (1-2 lines), then execute step by step.\n"
                "- After each tool call, check the result before proceeding.\n"
                "- If a tool fails, try a different approach instead of repeating.\n"
                "- Verify your work: re-read files you wrote, test scripts you created.\n"
                "- For current events/news: use web_search immediately.\n"
                "- For URLs the user provides: use web_fetch to read them.\n"
                "- Don't ask 'should I do X?' — just do it if the intent is clear."
            )
            messages.append({"role": "system", "content": tool_prompt})

        # 4. Few-shot examples (SillyTavern: mesExamples)
        examples = agent.get("examples", "")
        if examples:
            messages.append({"role": "system", "content": f"[Example Chat]\n{examples}"})

        # 5. Chat history with name field attribution
        messages.extend(self._history_to_messages(agent_name))

        # 6. Post-history instructions (SillyTavern: jailbreak position)
        instructions = agent.get("instructions", "")
        if instructions:
            messages.append({"role": "system", "content": instructions})

        # 7. Leader-specific instructions
        if self._leader == agent_name:
            tool_names = "web_search, web_fetch, exec, read_file, write_file, edit_file, list_dir"
            members = [a for a in self._active_agents if a != agent_name]
            leader_prompt = (
                f"[你是 GROUP LEADER 👑。你的职责：\n"
                f"- 分析用户请求，制定计划\n"
                f"- 其他 agent 已先发言，请审阅他们的回复\n"
                f"- 纠正错误信息，补充遗漏，去重整合\n"
                f"- 给出最终汇总回复\n\n"
                f"当前轮数: {self._round}\n"
                f"可用工具: {tool_names}\n"
                f"团队成员: {', '.join(members)}]"
            )
            messages.append({"role": "system", "content": leader_prompt})

        # 8. Group nudge — ALWAYS last (SillyTavern: group_nudge_prompt)
        nudge = (
            f"[Write the next reply only as {agent_name}. "
            f"Do NOT write dialogue for other characters. "
            f"Stay in character and respond naturally.]"
        )
        messages.append({"role": "system", "content": nudge})

        return messages

    async def _agent_speak(self, agent_name: str) -> None:
        if agent_name not in self.registry:
            return
        model = self.registry[agent_name]["model"]
        messages = self._build_agent_prompt(agent_name)

        try:
            content, tools_used, stats = await self._chat_with_tools(
                messages=messages,
                model=model,
                agent_name=agent_name,
            )
            self._request_log.append({
                "agent": agent_name, "model": model,
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": len(content), "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "group", "tools": tools_used,
                "input_preview": (self._history[-2]["content"][:200] if len(self._history) >= 2 else ""),
                "output": content[:500],
                **stats,
            })
            if content:
                self._add_message(agent_name, content)
                total = len(self._active_agents)
                idx = self._active_agents.index(agent_name) + 1 if agent_name in self._active_agents else 0
                badge = " 👑" if self._leader == agent_name else ""
                round_tag = f" [{idx}/{total}]" if total > 1 else ""
                await self._send(f"💬 {agent_name}{badge}{round_tag}:\n\n{content}")
        except Exception as e:
            logger.error("Groupchat: {} LLM call failed: {}", agent_name, e)
            self._request_log.append({
                "agent": agent_name, "model": model,
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": 0, "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "group", "error": str(e),
            })
            await self._send(f"⚠️ {agent_name} 回复失败: {e}")

    async def _generate_summary(self) -> None:
        if not self._history:
            return
        # Use first active agent's model
        agent_name = self._active_agents[0] if self._active_agents else list(self.registry.keys())[0]
        model = self.registry[agent_name]["model"]

        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": "你是一个讨论总结专家。"},
                    {"role": "user", "content": (
                        f"话题：{self._topic}\n\n"
                        f"群聊记录：\n{self._format_history()}\n\n"
                        "请输出简洁总结：1)核心观点 2)分歧点 3)初步结论"
                    )},
                ],
                model=model,
                max_tokens=2000,
            )
            summary = response.content or "无法生成总结"
            await self._send(f"📋 讨论总结:\n\n{summary}")
        except Exception as e:
            logger.error("Summary failed: {}", e)
            await self._send(f"⚠️ 总结生成失败: {e}")

    async def _run_loop(self) -> None:
        """Main group chat loop — runs while 2+ agents are active."""
        try:
            await self._send(
                f"🎭 群聊模式！\n"
                f"👥 成员: {', '.join(self._active_agents)}\n"
                f"📌 直接发消息，所有 agent 会轮流回复"
            )

            if not any(m["sender"] == "系统" for m in self._history):
                self._add_message("系统", f"话题：{self._topic}")

            rounds = 0
            while self._running and rounds < self.config.max_rounds:
                rounds += 1

                # Wait for user input (block until user sends something)
                user_input = None
                while self._running:
                    try:
                        user_input = await asyncio.wait_for(self._input_queue.get(), timeout=1.0)
                        break
                    except asyncio.TimeoutError:
                        continue

                if not self._running or not user_input:
                    break

                if user_input == "__SUMMARY__":
                    await self._generate_summary()
                    continue

                # Record user message (no echo)
                self._add_message("用户", user_input)
                self._round = rounds

                # Determine speaking order
                current_agents = list(self._active_agents)
                if self._leader and self._leader in current_agents:
                    # Leader mode: others first, leader last
                    others = [a for a in current_agents if a != self._leader]
                    speak_order = others + [self._leader]
                else:
                    speak_order = current_agents

                for name in speak_order:
                    if not self._running or name not in self._active_agents:
                        break
                    await asyncio.sleep(self.config.auto_reply_delay)
                    await self._agent_speak(name)

            if self._running:
                await self._send("🔚 群聊结束！正在生成总结...")
                await self._generate_summary()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Group chat loop error: {}", e)
            await self._send(f"❌ 群聊异常: {e}")
        finally:
            self._running = False
            logger.info("Group chat loop ended")
