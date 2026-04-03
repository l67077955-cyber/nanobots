"""engine.py — 群聊核心引擎（GroupChatEngine）。

这是整个群聊系统的中心枢纽，管理 agent 注册表、对话历史、消息分发。

╔══════════════════════════════════════════════════════════════╗
║  文件关系图（谁调用谁）:                                      ║
║                                                              ║
║  engine.py (本文件)                                          ║
║    ├─ run_loop.py     → 主循环，等待用户输入后调用 broadcast  ║
║    ├─ broadcast.py    → 广播模式协调器，管理 agent 任务       ║
║    ├─ agent_runner.py → 单个 agent 的 tool_loop 执行器       ║
║    ├─ prompt_builder.py → 构建 agent 的 prompt/messages       ║
║    ├─ state_bus.py    → state.yaml 读写（leader 控制面板）   ║
║    ├─ mailbox.py      → agent 间消息传递                     ║
║    ├─ speaker.py      → 单 agent 发言逻辑（1v1模式用）       ║
║    ├─ display.py      → 所有显示格式化函数                   ║
║    ├─ chatroom_tools.py → chatroom_send/wait 工具定义        ║
║    └─ search_tools.py → SmartSearch/SmartFetch 工具包装      ║
╚══════════════════════════════════════════════════════════════╝

⚠️ agent 修改本文件时注意：
    1. registry 是 dict[str, dict] — 每个 agent 的配置（model, prompt, tools 等）
    2. _active_agents 是 list[str] — 当前参与群聊的 agent 名单
    3. _history 是 list[dict] — 格式 {"sender": "xxx", "content": "xxx"}
    4. _send() 是显示消息给用户的唯一通道，sender 参数用于禁言检查
    5. _leader 是 leader agent 名字（str | None）
"""

from __future__ import annotations

import asyncio
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
        from nanobot.groupchat.search_tools import SmartFetchTool, SmartSearchTool

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
        self._muted_agents: set[str] = set()  #禁言列表
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
            save_event=None,
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
        
        if hasattr(self, '_state_bus') and self._state_bus:
            try:
                def mutate_stop(data):
                    if "session" in data:
                        data["session"]["status"] = "stopped"
                self._state_bus._update(mutate_stop)
            except Exception as e:
                logger.warning("Failed to mark session as stopped: {}", e)

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

        # Auto-resume the most recent active session
        sessions_dir = Path.home() / ".nanobot" / "collab-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        
        self._session_dir = None
        try:
            # Sort sessions by modified time, latest first
            all_sessions = sorted(sessions_dir.glob("gc-*/state.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
            for state_file in all_sessions:
                import yaml
                try:
                    with open(state_file, 'r', encoding='utf-8') as f:
                        data = yaml.safe_load(f)
                    if data and "session" in data:
                        status = data["session"].get("status", "unknown")
                        if status == "running":
                            self._session_dir = state_file.parent
                            logger.info("Groupchat: resuming active session {}", self._session_dir.name)
                            break
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Error checking active sessions: {}", e)

        if not self._session_dir:
            timestamp = _cn_now().strftime("%Y%m%d-%H%M%S")
            self._session_dir = sessions_dir / f"gc-{timestamp}"
            self._session_dir.mkdir(parents=True, exist_ok=True)

        # Initialize FileStateBus — single state.yaml per session
        # The broadcast coordinator creates its own bus for broadcast rounds;
        # this one supplies the persistence layer for conversation sync.
        from nanobot.groupchat.state_bus import FileStateBus
        self._state_bus = FileStateBus(self._session_dir)
        self._state.state_bus = self._state_bus

        # Session metadata is now in state.yaml (written by broadcast.setup)

        self._task = asyncio.create_task(self._run_loop())
        logger.info("Group chat loop started with {}", self._active_agents)

    def _stop_group_loop(self) -> None:
        """Stop the group chat loop (keeps active agents and history)."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _send(self, text: str, *, sender: str = "") -> None:
        #禁言检查：被禁言的agent不展示消息
        if sender and sender in self._muted_agents:
            logger.debug("Groupchat: {} is muted, skipping display", sender)
            return
        if self._send_fn:
            try:
                await self._send_fn(text)
            except Exception as e:
                logger.error("Groupchat send failed: {}", e)

    # ── Mute / Unmute ──────────────────────────────────────────

    def mute_agent(self, name: str) -> None:
        """Mute an agent — their messages will not be displayed to the user."""
        self._muted_agents.add(name)
        logger.info("Groupchat: {} muted", name)

    def unmute_agent(self, name: str) -> None:
        """Unmute an agent — restore message display."""
        self._muted_agents.discard(name)
        logger.info("Groupchat: {} unmuted", name)

    def is_muted(self, name: str) -> bool:
        """Check if an agent is currently muted."""
        return name in self._muted_agents

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
            from nanobot.groupchat.history_settings import max_messages
            limit = max_messages()
        except Exception:
            limit = self.config.max_history
        if len(self._history) > limit:
            self._history = self._history[-limit:]
        self._state.save_message(sender, content, self._history)

    def _on_agent_comm(self, sender: str, targets: list[str], content: str) -> None:
        """Callback from MailboxHub.send() — deliver to state_bus."""
        if hasattr(self, '_state_bus') and self._state_bus:
            try:
                self._state_bus.deliver_message(sender, targets, content, all_agents=self._active_agents)
            except Exception:
                pass



    def _build_agent_prompt(self, agent_name: str, context_exclude: list[int] | None = None) -> list[dict[str, Any]]:
        """Build prompt — delegates to PromptBuilder, with skills injected.

        Args:
            context_exclude: Conversation seq numbers to hide from this agent.
        """
        messages = self._prompt_builder.build_agent_prompt(
            agent_name,
            registry=self.registry,
            active_agents=self._active_agents,
            history=self._history,
            leader=self._leader,
            round_num=self._round,
            context_exclude=context_exclude,
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

    async def _run_loop(self) -> None:
        """Main group chat loop — delegates to run_loop module."""
        from nanobot.groupchat.run_loop import run_loop
        await run_loop(self)

