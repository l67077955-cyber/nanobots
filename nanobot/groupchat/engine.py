"""Async group chat engine for multi-agent discussions.

Supports fluid agent management:
- Agent registry: all available agents (loaded from config/directory)
- Active participants: agents currently in the conversation
- Seamlessly transitions between 1-on-1 and group chat as agents are added/removed
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger


from nanobot.groupchat.agents import load_agents
from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.mailbox import MailboxHub
from nanobot.groupchat.persistence import GroupChatState
from nanobot.groupchat.prompt_builder import PromptBuilder
from nanobot.groupchat.response_cleanup import clean_response as _clean_response_fn
from nanobot.groupchat.utils import cn_now as _cn_now
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
        web_search_config: Any = None,
        web_proxy: str | None = None,
        cron_service: Any | None = None,
    ):
        self.config = config
        self.provider = provider
        self.workspace = workspace
        self.web_search_config = web_search_config
        self.web_proxy = web_proxy
        self._cron_service = cron_service
        self._pm_cache: dict | None = None
        self._mailbox = MailboxHub(on_message=self._on_agent_comm)
        self._prompt_builder = PromptBuilder(config=config, workspace=workspace)

        # Skills system (shared with single-chat)
        from nanobot.agent.skills import SkillsLoader
        self._skills = SkillsLoader(workspace)
        self._register_tools()
        self._init_state()

    # ── Workspace scope presets ────────────────────────────────
    _WORKSPACE_PRESETS = {"source", "workspace", "prompts", "tmp"}

    def _resolve_agent_workspace(self, agent_name: str) -> Path:
        """Resolve an agent's workspace path from its workspace_scope config.

        Presets:
            "source"    → nanobot source code root (auto-detected)
            "workspace" → user-configured workspace (default)
            "prompts"   → agent's own directory (~/.nanobot/agents/<name>)
            "tmp"       → /tmp sandbox
            <abs path>  → literal path
        """
        agent_cfg = self.registry.get(agent_name, {})
        scope = agent_cfg.get("workspace_scope", "workspace")

        if scope == "workspace":
            return self.workspace
        elif scope == "source":
            # Auto-detect nanobot source root from package location
            import nanobot
            return Path(nanobot.__file__).parent.parent
        elif scope == "prompts":
            agent_dir = agent_cfg.get("agent_dir")
            return Path(agent_dir) if agent_dir else self.workspace
        elif scope == "tmp":
            return Path("/tmp")
        elif scope.startswith("/"):
            return Path(scope)
        else:
            logger.warning("Unknown workspace scope '{}' for {}, using workspace", scope, agent_name)
            return self.workspace

    def _register_tools(self) -> None:
        """Register default tool registries and per-agent registry cache."""
        # Lazy import to avoid circular: engine → agent.tools → agent → config → groupchat
        from nanobot.agent.tools.registry import ToolRegistry

        ws = self.workspace

        # Default group chat tools (used for agents without custom workspace)
        self.tools = self._build_tool_registry(ws)

        # Register chatroom tools on default registry
        from nanobot.groupchat.chatroom_tools import ChatroomSendTool, WaitTool
        self._chatroom_send_tool = ChatroomSendTool(mailbox=self._mailbox)
        self._wait_tool = WaitTool(mailbox=self._mailbox)
        self.tools.register(self._chatroom_send_tool)
        self.tools.register(self._wait_tool)

        # Direct chat tools (default agent — workspace scope)
        self.direct_tools = self._build_tool_registry(ws)

        # Per-agent registry cache {workspace_path_str: ToolRegistry}
        self._tool_registry_cache: dict[str, ToolRegistry] = {
            str(ws): self.tools,
        }

        logger.info("Groupchat: registered {} group tools, {} direct tools",
                    len(self.tools), len(self.direct_tools))

        # CronTool removed — cron is now a pure skill (skills/cron/scripts/cron_cli.py)

    def _build_tool_registry(self, ws: Path) -> "ToolRegistry":
        """Build a ToolRegistry scoped to the given workspace path."""
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
        from nanobot.agent.tools.shell import ExecTool
        from nanobot.agent.tools.filesystem import (
            ReadFileTool, WriteFileTool, EditFileTool, ListDirTool,
        )
        from nanobot.groupchat.chatroom_tools import SmartFetchTool, SmartSearchTool

        registry = ToolRegistry()
        raw_search = WebSearchTool(config=self.web_search_config, proxy=self.web_proxy)
        registry.register(SmartSearchTool(raw_search, provider=self.provider))
        # Wrap web_fetch with AI reader — model configurable via agents/reader/
        raw_fetch = WebFetchTool()
        reader_model = self._get_reader_model()
        registry.register(SmartFetchTool(raw_fetch, reader_model=reader_model, provider=self.provider))
        registry.register(ExecTool(timeout=120, working_dir=str(ws)))
        from nanobot.agent.tools.process import ProcessTool
        registry.register(ProcessTool())
        registry.register(ReadFileTool(workspace=ws, allowed_dir=None))
        registry.register(WriteFileTool(workspace=ws, allowed_dir=ws))
        registry.register(EditFileTool(workspace=ws, allowed_dir=ws))
        registry.register(ListDirTool(workspace=ws, allowed_dir=None))
        return registry

    def _get_reader_model(self) -> str:
        """Read the reader agent's model from config. Falls back to default."""
        import json as _json
        reader_cfg_path = Path.home() / ".nanobot" / "agents" / "reader" / "config.json"
        default_model = "openai/gpt-4.1-nano"
        if reader_cfg_path.exists():
            try:
                cfg = _json.loads(reader_cfg_path.read_text())
                model = (
                    cfg.get("model")
                    or cfg.get("agents", {}).get("defaults", {}).get("model")
                )
                if model:
                    return model
            except Exception:
                pass
        return default_model

    def _get_agent_registry(self, agent_name: str) -> "ToolRegistry":
        """Get (or build and cache) the tool registry for an agent's workspace scope."""
        ws = self._resolve_agent_workspace(agent_name)
        key = str(ws)
        if key not in self._tool_registry_cache:
            reg = self._build_tool_registry(ws)
            # Add chatroom tools to custom registries too
            from nanobot.groupchat.chatroom_tools import ChatroomSendTool, WaitTool
            reg.register(ChatroomSendTool(mailbox=self._mailbox))
            reg.register(WaitTool(mailbox=self._mailbox))
            self._tool_registry_cache[key] = reg
            logger.info("Groupchat: built tool registry for {} → {}", agent_name, ws)
        return self._tool_registry_cache[key]

    def _init_state(self) -> None:
        """Load agent registry, persistence layer, and initialize runtime state."""
        # Agent registry: all known agents {name: {model, prompt}}
        self.registry: dict[str, dict[str, Any]] = load_agents(self.config, self.workspace)

        # Persistence layer
        self._state = GroupChatState(
            registry=self.registry,
            default_mode=self.config.mode or "serial",
        )

        # Restore persisted state
        self._active_agents: list[str] = self._state.load_active()
        self._leader: str | None = self._state.load_leader()
        self._mode: str = self._state.load_mode()
        self._round: int = 0

        # Runtime state (ephemeral, not persisted)
        self._task: asyncio.Task | None = None
        self._running = False
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._send_fn: Callable[[str], Awaitable[None]] | None = None
        self._edit_fn: Callable[[int, str], Awaitable[None]] | None = None
        self._on_round_done: Callable[[], Awaitable[None]] | None = None
        self._send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None = None
        self._topic: str = ""
        self._history: list[dict[str, str]] = []
        self._request_log: list[dict[str, Any]] = []
        self._debug_context: bool = False
        self._prompt_order: dict[str, list[str]] = self._prompt_builder._load_prompt_order()

        # Direct chat interjection state (single-agent mode)
        self._direct_chat_task: asyncio.Task | None = None
        self._direct_chat_queue: asyncio.Queue[str] = asyncio.Queue()

    # ── Public access to PromptBuilder ────────────────────────

    @property
    def prompt_builder(self) -> PromptBuilder:
        """Public access to the prompt builder (used by telegram, broadcast)."""
        return self._prompt_builder

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

    def set_tool_context(self, channel: str, chat_id: str) -> None:
        """Set channel/chat context for cron CLI script via env vars.

        Mirrors AgentLoop._set_tool_context() — called once when the
        Telegram channel wires its send callback.
        """
        import os
        os.environ["NANOBOT_CHANNEL"] = channel
        os.environ["NANOBOT_CHAT_ID"] = chat_id

    def set_send_fn(self, send_fn: Callable[[str], Awaitable[None]]) -> None:
        """Set the message output callback."""
        self._send_fn = send_fn

    def set_edit_fn(
        self,
        edit_fn: Callable[[int, str], Awaitable[None]],
        send_and_get_id_fn: Callable[[str], Awaitable[int | None]],
    ) -> None:
        """Set callbacks for streaming message edits."""
        self._edit_fn = edit_fn
        self._send_and_get_id_fn = send_and_get_id_fn

    def set_on_round_done(self, cb: Callable[[], Awaitable[None]]) -> None:
        """Set callback invoked when all agents finish speaking in a round."""
        self._on_round_done = cb

    # ── Public API (used by channels) ─────────────────────────

    @property
    def mode(self) -> str:
        """Current chat mode (e.g. 'broadcast', 'orchestra')."""
        return self._mode

    @property
    def history(self) -> list[dict[str, str]]:
        """Chat message history."""
        return self._history

    @property
    def request_log(self) -> list[dict[str, Any]]:
        """LLM request log for the current session."""
        return self._request_log

    @property
    def has_send_fn(self) -> bool:
        """Whether a send callback is registered."""
        return self._send_fn is not None

    @property
    def has_edit_fn(self) -> bool:
        """Whether edit callbacks are registered."""
        return self._edit_fn is not None

    @property
    def has_on_round_done(self) -> bool:
        """Whether a round-done callback is registered."""
        return self._on_round_done is not None

    def resolve_agent_name(self, name: str) -> str | None:
        """Case-insensitive agent name lookup. Returns canonical name or None."""
        return self._resolve_agent_name(name)

    def load_groups(self) -> dict[str, list[str]]:
        """Load saved agent groups from disk."""
        return self._state.load_groups()

    def save_groups(self, groups: dict[str, list[str]]) -> None:
        """Save agent groups to disk."""
        self._state.save_groups(groups)

    def save_active(self) -> None:
        """Persist the current active agents list to disk."""
        self._state.save_active(self._active_agents)

    def clear_history(self) -> None:
        """Clear conversation history and request log, but keep active agents and loop running."""
        self._history.clear()
        self._request_log.clear()
        self._session_dir = None

    def reset(self) -> None:
        """Clear history, request log, active agents, and stop the loop."""
        self.stop()
        self._history.clear()
        self._request_log.clear()
        self._active_agents.clear()
        self._session_dir = None

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
        self._state.save_active(self._active_agents)
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
        self._state.save_active(self._active_agents)
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
        self._state.save_active(self._active_agents)
        # Auto-update saved group if one is loaded
        if hasattr(self, '_current_group_name') and self._current_group_name:
            groups = self._state.load_groups()
            if self._current_group_name in groups:
                groups[self._current_group_name] = list(resolved)
                self._state.save_groups(groups)
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
            self._state.save_leader(None)
            return f"✅ 已取消 Leader 模式" + (f" ({old})" if old else "")

        matched = self._resolve_agent_name(name)
        if not matched:
            return f"❌ Agent '{name}' 不存在。可用: {', '.join(self.registry.keys())}"

        self._leader = matched
        self._state.save_leader(matched)
        return f"👑 {matched} 已设为 Leader\n其他 agent 会先发言，{matched} 最后汇总"

    # ── Mode Management (serial / broadcast) ────────────────

    def set_mode(self, mode: str) -> str:
        """Switch group chat execution mode."""
        mode = mode.lower().strip()
        if mode not in ("serial", "broadcast"):
            return f"❌ 未知模式 '{mode}'，可选: serial, broadcast"
        old = self._mode
        self._mode = mode
        self._state.save_mode(mode)
        labels = {"serial": "串行轮流", "broadcast": "广播乱序"}
        return f"✅ 模式切换: {labels.get(old, old)} → {labels.get(mode, mode)}"


    def save_group(self, name: str) -> str:
        """Save current active agents as a named group."""
        if not self._active_agents:
            return "⚠️ 没有活跃 agent，无法保存"
        groups = self._state.load_groups()
        groups[name] = list(self._active_agents)
        self._state.save_groups(groups)
        return f"✅ 已保存分组 「{name}」\n👥 成员: {', '.join(self._active_agents)}"

    def load_group(self, name: str) -> str:
        """Load a saved group config, setting agents directly."""
        groups = self._state.load_groups()
        if name not in groups:
            available = ', '.join(groups.keys()) if groups else '无'
            return f"⚠️ 分组 「{name}」 不存在\n📋 可用分组: {available}"

        target = groups[name]

        # Stop any running loop first
        self._stop_group_loop()

        # Set agents directly (no add/remove to avoid loop race)
        valid = [a for a in target if self._resolve_agent_name(a)]
        self._active_agents = valid
        self._state.save_active(self._active_agents)

        # Start group loop if 2+ agents
        if len(self._active_agents) >= 2:
            self._start_group_loop()

        self._current_group_name = name
        # Build rich output
        lines = [f"✅ 已载入分组 「{name}」"]
        leader = self._leader
        if leader:
            lines.append(f"👑 领导者: {leader}")
        lines.append(f"📢 发言顺序: {' → '.join(self._active_agents)}")
        lines.append("")
        for a in self._active_agents:
            info = self.registry.get(a, {})
            model = info.get('model', '?')
            badge = " 👑" if leader == a else ""
            lines.append(f"  {a}{badge}: 🤖 {model}")
        return "\n".join(lines)

    def delete_group(self, name: str) -> str:
        """Delete a saved group config."""
        groups = self._state.load_groups()
        if name not in groups:
            return f"⚠️ 分组 「{name}」 不存在"
        del groups[name]
        self._state.save_groups(groups)
        return f"🗑 已删除分组 「{name}」"

    def list_groups(self) -> str:
        """List all saved group configs."""
        groups = self._state.load_groups()
        if not groups:
            return "📋 没有保存的分组\n用 /savegroup 保存当前成员"
        lines = ["📋 已保存的分组：\n"]
        for gname, members in groups.items():
            order = " → ".join(members)
            member_info = []
            for m in members:
                info = self.registry.get(m, {})
                model = info.get('model', '?').split('/')[-1]
                member_info.append(f"{m}({model})")
            lines.append(f"  📁 {gname}")
            lines.append(f"     {' → '.join(member_info)}")
        return "\n".join(lines)

    # ── Response cleanup ───

    def _clean_response(self, content: str, agent_name: str) -> str:
        """Clean up model response — delegates to response_cleanup module."""
        return _clean_response_fn(content, agent_name, list(self.registry.keys()))

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
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_content_reset: Callable[[], Awaitable[None]] | None = None,
        on_tool_start_override: Callable | None = None,
        on_tool_result_override: Callable | None = None,
        force_no_tools: bool = False,
    ) -> tuple[str, list[str], dict[str, Any]]:
        """Chat with tool calling loop — delegates to tool_chat module.

        Returns (content, tools_used, stats).
        """
        from nanobot.groupchat.tool_chat import chat_with_tools

        # Tool selection — use per-agent registry based on workspace_scope
        agent_cfg = self.registry.get(agent_name, {})
        tool_registry = self.direct_tools if is_direct else self._get_agent_registry(agent_name)
        tool_defs = self._get_agent_tools(agent_cfg, tool_registry)

        # Set chatroom tool context for this agent
        if hasattr(self, '_chatroom_send_tool'):
            self._chatroom_send_tool.set_agent(agent_name)
            self._wait_tool.set_agent(agent_name)
            self._mailbox.create(agent_name)

        session_id = self._session_dir.name if self._session_dir else "direct"

        return await chat_with_tools(
            provider=self.provider,
            messages=messages,
            model=model,
            agent_name=agent_name,
            tool_registry=tool_registry,
            tool_defs=tool_defs,
            max_tokens=self.config.max_tokens,
            max_iterations=max_iterations,
            session_id=session_id,
            is_direct=is_direct,
            debug_context=self._debug_context,
            topic=self._topic or "",
            clean_response=lambda c: self._clean_response(c, agent_name),
            on_content_delta=on_content_delta,
            on_content_reset=on_content_reset,
            on_tool_start_override=on_tool_start_override,
            on_tool_result_override=on_tool_result_override,
            save_event=self._save_event,
            send_fn=self._send_fn,
            send_and_get_id_fn=self._send_and_get_id_fn,
            edit_fn=self._edit_fn,
            force_no_tools=force_no_tools,
        )

    def direct_chat_inject(self, user_message: str) -> bool:
        """Inject a user interjection into an in-progress direct chat.

        Returns True if injected, False if no direct chat is running.
        """
        if self._direct_chat_task and not self._direct_chat_task.done():
            self._direct_chat_queue.put_nowait(user_message)
            logger.info("Direct chat: interjection queued ({} chars)", len(user_message))
            return True
        return False

    async def direct_chat(self, user_message: str) -> str | None:
        """Send message to single active agent — delegates to direct_chat module."""
        from nanobot.groupchat.direct_chat import direct_chat as _direct_chat
        return await _direct_chat(self, user_message)

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
        self._state.save_active(self._active_agents)

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

        # Write session metadata to structured log
        self._save_event("session_start", extra={
            "agents": list(self._active_agents),
            "mode": self._mode,
            "topic": self._topic,
            "leader": self._leader,
            "models": {n: self.registry.get(n, {}).get("model", "?") for n in self._active_agents},
        })

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

    @property
    def _session_dir(self) -> Path | None:
        """Delegate session_dir to persistence layer."""
        return self._state.session_dir

    @_session_dir.setter
    def _session_dir(self, value: Path | None) -> None:
        self._state.session_dir = value

    def _add_message(self, sender: str, content: str) -> None:
        self._history.append({"sender": sender, "content": content})
        try:
            from nanobot.groupchat.history_settings import max_messages, max_context_chars
            limit = max_messages()
            char_budget = max_context_chars()
        except Exception:
            limit = self.config.max_history
            char_budget = 0

        # Step 1: 条数上限（保留最近 N 条）
        if len(self._history) > limit:
            self._history = self._history[-limit:]

        # Step 2: 字符预算裁剪（从尾部往前累加，超预算就截掉头部）
        if char_budget > 0:
            total = 0
            cutoff = 0
            for i in range(len(self._history) - 1, -1, -1):
                total += len(self._history[i]["content"])
                if total > char_budget:
                    cutoff = i + 1
                    break
            if cutoff > 0:
                self._history = self._history[cutoff:]

        self._state.save_message(sender, content, self._history)

    def _save_event(
        self,
        event_type: str,
        *,
        agent: str = "",
        content: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Delegate to persistence layer."""
        self._state.save_event(event_type, agent=agent, content=content, extra=extra)

    def _save_round_summary(
        self,
        round_num: int,
        agents_responded: int,
        comm_count: int = 0,
        duration: float = 0.0,
    ) -> None:
        """Delegate to persistence layer."""
        self._state.save_round_summary(round_num, agents_responded, comm_count, duration)

    def _on_agent_comm(self, sender: str, targets: list[str], content: str) -> None:
        """Callback from MailboxHub.send() — persist agent communication."""
        self._state.save_event("agent_comm", agent=sender, content=content, extra={
            "targets": targets,
        })

    async def _maybe_compress_history(self) -> None:
        """Summarize the oldest half of history when approaching the message limit.

        Triggered when history exceeds 80% of max_messages. The earliest
        messages are replaced by a single system summary, keeping recent
        messages intact for full context.
        """
        from nanobot.groupchat.history_settings import (
            max_messages, summarize_enabled, summarize_model,
        )

        if not summarize_enabled():
            return
        limit = max_messages()
        if len(self._history) < int(limit * 0.8):
            return

        half = len(self._history) // 2
        to_compress = self._history[:half]
        keep = self._history[half:]

        history_text = "\n".join(
            f"[{m['sender']}]: {m['content']}" for m in to_compress
        )
        prompt = (
            f"以下是群聊的早期历史记录（共 {len(to_compress)} 条）。\n"
            f"请用简洁的中文摘要这些内容，保留关键决策、重要事实和用户的核心需求。\n"
            f"摘要不超过 500 字。\n\n{history_text}"
        )

        try:
            response = await self.provider.chat_with_retry(
                messages=[{"role": "user", "content": prompt}],
                model=summarize_model(),
                max_tokens=600,
            )
            summary = (response.content or "").strip()
            if summary:
                summary_msg = {
                    "sender": "系统",
                    "content": f"[早期对话摘要（共 {len(to_compress)} 条消息）]\n{summary}",
                }
                self._history = [summary_msg] + keep
                logger.info(
                    "History compressed: {} messages → summary + {} recent",
                    len(to_compress), len(keep),
                )
        except Exception as e:
            logger.warning("History compression failed: {}", e)

    def _format_history(self) -> str:
        return "\n\n".join(f"[{m['sender']}]: {m['content']}" for m in self._history)

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

    def _build_agent_prompt(
        self,
        agent_name: str,
        relevant_agents: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build prompt — delegates to PromptBuilder, with skills injected."""
        messages = self._prompt_builder.build_agent_prompt(
            agent_name,
            registry=self.registry,
            active_agents=self._active_agents,
            history=self._history,
            leader=self._leader,
            round_num=self._round,
            relevant_agents=relevant_agents,
        )

        # Inject skills (always-on content + summary of available skills)
        skills_parts: list[str] = []
        always_skills = self._skills.get_always_skills()
        if always_skills:
            content = self._skills.load_skills_for_context(always_skills)
            if content:
                skills_parts.append(content)
        summary = self._skills.build_skills_summary()
        if summary:
            skills_parts.append(
                "# Skills\n\n"
                "To use a skill, read its SKILL.md with read_file.\n\n"
                + summary
            )
        if skills_parts:
            messages.insert(1, {
                "role": "system",
                "content": "\n\n".join(skills_parts),
            })

        return messages

    async def _agent_speak(
        self,
        agent_name: str,
        synthesis_context: str | None = None,
        no_tools: bool = False,
        no_stream: bool = False,
        silent: bool = False,
    ) -> tuple[str, list[str], dict] | None:
        """Run one agent's turn — delegates to speaker module."""
        from nanobot.groupchat.speaker import agent_speak
        return await agent_speak(
            self, agent_name,
            synthesis_context=synthesis_context,
            no_tools=no_tools,
            no_stream=no_stream,
            silent=silent,
        )

    async def _generate_summary(self) -> None:
        """Generate discussion summary — delegates to run_loop module."""
        from nanobot.groupchat.run_loop import generate_summary
        await generate_summary(self)

    async def _run_loop(self) -> None:
        """Main group chat loop — delegates to run_loop module."""
        from nanobot.groupchat.run_loop import run_loop
        await run_loop(self)

