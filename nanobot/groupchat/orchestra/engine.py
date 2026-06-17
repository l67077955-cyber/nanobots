"""Async group chat engine for multi-agent discussions.

# Verified: Harper has write access to source.

Supports fluid agent management:
- Agent registry: all available agents (loaded from config/directory)
- Active participants: agents currently in the conversation
- Seamlessly transitions between 1-on-1 and group chat as agents are added/removed
"""

from __future__ import annotations

import asyncio
import random
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger


from nanobot.groupchat.history.agent_loader import load_agents
from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.orchestra.mailbox import MailboxHub
from nanobot.groupchat.history.persistence import GroupChatState
from nanobot.groupchat.history.context import HistoryContext
from nanobot.groupchat.history.prompt_builder import PromptBuilder
from nanobot.groupchat.history.response_cleanup import clean_response as _clean_response_fn
from nanobot.utils.helpers import cn_now as _cn_now
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
        "memory_palace", "forget",
    ]

    def __init__(
        self,
        config: GroupChatConfig,
        provider: LLMProvider,
        workspace: Path,
        web_search_config: Any = None,
        web_proxy: str | None = None,
        cron_service: Any | None = None,
        send_outbound_fn: Any | None = None,
        mcp_servers: dict | None = None,
    ):
        self.config = config
        self.provider = provider
        self.workspace = workspace
        self.web_search_config = web_search_config
        self.web_proxy = web_proxy
        self._cron_service = cron_service
        self._send_outbound_fn = send_outbound_fn  # Callable[[OutboundMessage], Awaitable[None]]

        self._mailbox = MailboxHub(on_message=self._on_agent_comm)
        self._prompt_builder = PromptBuilder(config=config, workspace=workspace)

        # MCP servers (lazy-connected on first run)
        self._mcp_servers: dict = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected: bool = False
        self._mcp_connecting: bool = False

        self._register_tools()
        self._init_state()

    # ── Workspace scope resolution ────────────────────────────────

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
        from nanobot.tools.registry import ToolRegistry

        ws = self.workspace

        # Default group chat tools (used for agents without custom workspace)
        self.tools = self._build_tool_registry(ws)

        # Register chatroom tools on default registry
        from nanobot.groupchat.orchestra.tools.chatroom_tools import ChatroomSendTool, WaitTool
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
        from nanobot.tools.registry import ToolRegistry
        from nanobot.tools.web import WebFetchTool, WebSearchTool
        from nanobot.tools.shell import ExecTool
        from nanobot.tools.filesystem import (
            ReadFileTool, WriteFileTool, EditFileTool, ListDirTool,
        )
        from nanobot.groupchat.orchestra.tools.chatroom_tools import SmartFetchTool, SmartSearchTool

        registry = ToolRegistry()
        raw_search = WebSearchTool(config=self.web_search_config, proxy=self.web_proxy)
        registry.register(SmartSearchTool(raw_search, provider=self.provider))
        # Wrap web_fetch with AI reader — model configurable via agents/reader/
        raw_fetch = WebFetchTool()
        reader_model = self._get_reader_model()
        registry.register(SmartFetchTool(raw_fetch, reader_model=reader_model, provider=self.provider))
        registry.register(ExecTool(timeout=120, working_dir=str(ws)))
        from nanobot.tools.process import ProcessTool
        registry.register(ProcessTool())
        registry.register(ReadFileTool(workspace=ws, allowed_dir=None))
        registry.register(WriteFileTool(workspace=ws, allowed_dir=None))
        registry.register(EditFileTool(workspace=ws, allowed_dir=None))
        registry.register(ListDirTool(workspace=ws, allowed_dir=None))
        # Register MessageTool so agents can send files (PDFs, images, etc.) to the user.
        # The send_outbound_fn is wired in from the gateway (bus.publish_outbound).
        # channel/chat_id are injected later via set_tool_context → _update_message_tool_context.
        if self._send_outbound_fn:
            from nanobot.tools.message import MessageTool
            registry.register(MessageTool(send_callback=self._send_outbound_fn))
        return registry

    def _sync_forget_tool(self, registry: "ToolRegistry", agent_name: str) -> None:
        """Register or unregister ForgetTool according to per-agent config."""
        from nanobot.groupchat.tool_policy import forget_tool_enabled
        from nanobot.tools.forget import ForgetTool

        agent_cfg = self.registry.get(agent_name, {})
        session_cfg = getattr(self, "_session_tools_override", {}).get(agent_name)
        enabled = forget_tool_enabled(agent_cfg, session_override=session_cfg)
        if enabled:
            if registry.get("forget") is None:
                registry.register(ForgetTool())
        elif registry.get("forget") is not None:
            registry.unregister("forget")

    def _get_reader_model(self) -> str:
        """Read the reader agent's model from config. Falls back to default."""
        import json as _json
        reader_cfg_path = Path.home() / ".nanobot" / "agents" / "reader" / "config.json"
        default_model = "openai/gpt-4.1-nano"
        if reader_cfg_path.exists():
            try:
                cfg = _json.loads(reader_cfg_path.read_text())
                model = cfg.get("model")
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
            from nanobot.groupchat.orchestra.tools.chatroom_tools import ChatroomSendTool, WaitTool
            reg.register(ChatroomSendTool(mailbox=self._mailbox))
            reg.register(WaitTool(mailbox=self._mailbox))
            self._tool_registry_cache[key] = reg
            logger.info("Groupchat: built tool registry for {} → {}", agent_name, ws)
        reg = self._tool_registry_cache[key]
        self._sync_forget_tool(reg, agent_name)
        return reg

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers and inject tools into all registries (lazy, one-time)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            # Inject into all known registries (default tools + direct_tools + cache)
            all_registries = set()
            all_registries.add(id(self.tools))
            all_registries.add(id(self.direct_tools))
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            await connect_mcp_servers(self._mcp_servers, self.direct_tools, self._mcp_stack)
            # Also inject into any already-cached per-agent registries
            for key, reg in self._tool_registry_cache.items():
                if id(reg) not in all_registries:
                    all_registries.add(id(reg))
                    await connect_mcp_servers(self._mcp_servers, reg, self._mcp_stack)
            self._mcp_connected = True
            logger.info("GroupChat MCP: connected {} server(s), tools injected into {} registries",
                        len(self._mcp_servers), len(all_registries))
        except BaseException as e:
            logger.error("GroupChat MCP: failed to connect (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    async def _disconnect_mcp(self) -> None:
        """Cleanly disconnect all MCP servers."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except Exception as e:
                logger.warning("GroupChat MCP: error during disconnect: {}", e)
            self._mcp_stack = None
            self._mcp_connected = False

    def _init_state(self) -> None:
        """Load agent registry, persistence layer, and initialize runtime state."""
        # Agent registry: all known agents {name: {model, prompt}}
        self.registry: dict[str, dict[str, Any]] = load_agents(self.config, self.workspace)

        # Persistence layer
        self._state = GroupChatState(
            registry=self.registry,
        )

        # Restore persisted state
        self._active_agents: list[str] = self._state.load_active()
        self._leader: str | None = self._state.load_leader()
        self._round: int = 0

        # ── History: fully managed by HistoryContext (history module) ──
        self.history: HistoryContext = HistoryContext(
            state=self._state,
            provider=self.provider,
        )
        # Backwards-compat shim: code that still reads engine._history gets
        # the live messages list.  Writes (append/replace) should go through
        # self.history.add_message() or self.history.messages directly.
        self._history = self.history.messages

        # Runtime state (ephemeral, not persisted)
        self._task: asyncio.Task | None = None
        self._running = False
        # Broadcast round: per-agent tasks registered by broadcast_round()
        # so remove_agent() can cancel an in-flight agent mid-round.
        self._broadcast_tasks: dict[str, asyncio.Task] = {}
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        # Agents added via add_agent() while a broadcast round is running are
        # queued here so broadcast_round can pick them up and spawn tasks for them.
        self._pending_join_queue: asyncio.Queue[str] = asyncio.Queue()
        self._send_fn: Callable[[str], Awaitable[None]] | None = None
        self._edit_fn: Callable[[int, str], Awaitable[None]] | None = None
        self._on_round_done: Callable[[], Awaitable[None]] | None = None
        self._send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None = None
        self._topic: str = ""
        self._request_log: list[dict[str, Any]] = []
        self._debug_context: bool = False
        self._prompt_order: dict[str, list[str]] = self._prompt_builder._load_prompt_order()
        self._current_group_name: str | None = None
        self._session_tools_override: dict[str, dict] = {}

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
        # Also update MessageTool context in all cached registries so agents
        # can send files (PDFs, images) to the correct Telegram chat.
        self._update_message_tool_context(channel, chat_id)

    def _update_message_tool_context(self, channel: str, chat_id: str) -> None:
        """Push channel/chat_id into all MessageTool instances in the registry cache."""
        from nanobot.tools.message import MessageTool
        for reg in self._tool_registry_cache.values():
            mt = reg.get("message")
            if isinstance(mt, MessageTool):
                mt.set_context(channel, chat_id)

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
        self.history.clear()
        self._history = self.history.messages  # keep shim in sync
        self._request_log.clear()

    def reset(self) -> None:
        """Clear history, request log, active agents, and stop the loop."""
        self.stop()
        self.history.clear()
        self._history = self.history.messages  # keep shim in sync
        self._request_log.clear()
        self._active_agents.clear()

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
            return f"⚠️ {matched} 已在对话中\n👥 当前成员: {', '.join(self._active_agents)}"

        self._active_agents.append(matched)
        self._state.save_active(self._active_agents)
        logger.info("Groupchat: added agent {}, active={}", matched, self._active_agents)

        # If a broadcast round is currently running, notify it so the new agent
        # can be spawned immediately rather than waiting for the next round.
        if self._running and self._broadcast_tasks:
            self._pending_join_queue.put_nowait(matched)
            logger.info("Groupchat: queued {} for mid-round join", matched)

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

        # Cancel any in-flight broadcast task for this agent and notify the mailbox.
        # Without this, a removed agent keeps running in the background and its
        # stale output appears after the round ends.
        task = self._broadcast_tasks.pop(matched, None)
        if task and not task.done():
            task.cancel()
            logger.info("Groupchat: cancelled broadcast task for {}", matched)
        self._mailbox.mark_agent_done(matched)

        # If the departing agent was the leader, transfer the role to the last
        # remaining agent (who speaks last and is best positioned to synthesize),
        # or clear it entirely when no one is left.
        leader_note = ""
        if matched == self._leader:
            if self._active_agents:
                new_leader = self._active_agents[-1]
                self._leader = new_leader
                self._state.save_leader(new_leader)
                leader_note = f"\n👑 Leader 已转移给 {new_leader}"
                logger.info("Groupchat: leader transferred from {} to {}", matched, new_leader)
            else:
                self._leader = None
                self._state.save_leader(None)
                logger.info("Groupchat: leader {} left, leader mode cleared", matched)

        # Stop loop only when no agents left
        if not self._active_agents and self._running:
            self._stop_group_loop()
            return f"✅ {matched} 已离开，无活跃 agent" + leader_note

        return f"✅ {matched} 已离开\n👥 当前成员: {', '.join(self._active_agents)}" + leader_note

    def delete_agent(self, name: str) -> bool:
        """Permanently delete an agent from the registry and disk.
        
        Returns True if a directory was deleted.
        """
        matched = self._resolve_agent_name(name)
        if not matched:
            return False
            
        # 1. Remove from active agents
        if matched in self._active_agents:
            self.remove_agent(matched)
            
        # 2. Clear leader if needed
        if self._leader == matched:
            self.set_leader(None)
            
        # 3. Remove from saved groups
        groups = self._state.load_groups()
        changed = False
        for gname, members in groups.items():
            if matched in members:
                groups[gname] = [m for m in members if m != matched]
                changed = True
        if changed:
            self._state.save_groups(groups)
            
        # 4. Remove from registry
        if matched in self.registry:
            del self.registry[matched]
            
        # 5. Delete from disk
        import shutil
        agent_dir = Path.home() / ".nanobot" / "agents" / matched.lower()
        if agent_dir.exists():
            try:
                shutil.rmtree(agent_dir)
                return True
            except Exception as e:
                logger.warning("Failed to delete agent dir {}: {}", agent_dir, e)
        return False
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

    def get_agent_enabled_tool_names(self, agent_name: str) -> list[str]:
        """Get the names of tools currently enabled for an agent."""
        agent_cfg = self.registry.get(agent_name, {})
        # Session override takes precedence
        if hasattr(self, "_session_tools_override") and agent_name in self._session_tools_override:
            tools_cfg = self._session_tools_override[agent_name]
        else:
            tools_cfg = agent_cfg.get("tools")
            
        if isinstance(tools_cfg, dict):
            return [k for k, v in tools_cfg.items() if v]
        elif agent_cfg.get("tools_enabled", False) or agent_cfg.get("_default"):
            return list(self.TOOL_NAMES)
        return []

    def _get_agent_tools(self, agent_cfg: dict, registry, agent_name: str = None) -> list:
        """Get filtered tool definitions based on agent's per-tool config.

        Config format:
          tools: {web_search: true, exec: false, ...}  # granular
          tools_enabled: true  # legacy: all tools on
        """
        # Session override takes precedence
        if agent_name and hasattr(self, "_session_tools_override") and agent_name in self._session_tools_override:
            tools_cfg = self._session_tools_override[agent_name]
        else:
            tools_cfg = agent_cfg.get("tools")
            
        # Granular tools dict
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
        # Lazy-connect MCP servers (one-time, idempotent)
        await self._connect_mcp()

        from nanobot.groupchat.orchestra.engine import chat_with_tools

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

        # Per-agent hyperparams: fresh read from live registry as late as possible
        # so that changes made via /editagent (which directly mutates the dict
        # in registry + writes per-agent config.json) take effect on this turn
        # without restart. We pass the value down; the standalone function
        # uses it for the temporary override of provider.sampling_params.
        agent_sampling = None
        agent_cfg_data = self.registry.get(agent_name, {})
        if isinstance(agent_cfg_data.get("hyperparams"), dict):
            agent_sampling = dict(agent_cfg_data["hyperparams"])

        # NOTE on concurrency: broadcast runs multiple agent tasks concurrently.
        # The temp override below (and restore) on the shared provider can race
        # if two agents' LLM calls interleave. The per-turn snapshot + restore
        # mitigates leakage for sequential turns, but true isolation would require
        # threading the effective sampling all the way into the provider builders
        # instead of mutating self.sampling_params. For now we rely on the
        # short critical sections around individual provider.chat calls.
        return await chat_with_tools(
            provider=self.provider,
            messages=messages,
            model=model,
            agent_name=agent_name,
            tool_registry=tool_registry,
            tool_defs=tool_defs,
            max_tokens=self.config.max_tokens,
            agent_sampling=agent_sampling,
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
        from nanobot.groupchat.orchestra.engine import direct_chat as _direct_chat
        return await _direct_chat(self, user_message)

    def inject(self, message: str) -> None:
        """Inject a user message into the chat loop (1+ agents)."""
        # Lazy start: if any agents and loop not running, start it now
        if not self._running and len(self._active_agents) >= 1:
            self._start_group_loop()
        if self._running:
            self._input_queue.put_nowait(message)

    def request_summary(self) -> None:
        """Request a discussion summary."""
        if self._running:
            self._input_queue.put_nowait("__SUMMARY__")

    def stop(self) -> None:
        """Stop the group loop but keep active agents."""
        self._stop_group_loop()
        try:
            asyncio.ensure_future(self._disconnect_mcp())
        except RuntimeError:
            pass  # no event loop — shutdown scenario

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
            "mode": "broadcast",
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
        # Cancel any in-flight broadcast agent tasks so they don't keep running
        # after /stop. Without this, agents continue tool calls and send messages
        # even though the run loop has been cancelled.
        for name, task in list(self._broadcast_tasks.items()):
            if not task.done():
                task.cancel()
                logger.info("Groupchat: stop cancelled broadcast task for {}", name)
        self._broadcast_tasks.clear()

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
        """Append a message — delegates to HistoryContext."""
        self.history.add_message(sender, content)
        # Keep the shim alias in sync after HistoryContext may have rebuilt the list
        self._history = self.history.messages

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
        """Compress history if needed — delegates to HistoryContext."""
        await self.history.maybe_compress()
        self._history = self.history.messages  # keep shim in sync

    def _format_history(self) -> str:
        """Format history as string — delegates to HistoryContext."""
        return self.history.format()


    def _pick_next_speaker(self, last_content: str = "") -> str:
        names = self._active_agents
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
        *,
        agent_ranks: dict[str, int] | None = None,
        agent_idx: int | None = None,
        total: int | None = None,
        teammates: list[str] | None = None,
        user_question: str = "",
    ) -> list[dict[str, Any]]:
        """Build prompt — delegates entirely to PromptBuilder."""
        messages = self._prompt_builder.build_agent_prompt(
            agent_name,
            registry=self.registry,
            active_agents=self._active_agents,
            history=self._history,
            leader=self._leader,
            round_num=self._round,
            relevant_agents=relevant_agents,
            agent_ranks=agent_ranks,
            agent_idx=agent_idx,
            total=total,
            teammates=teammates,
            user_question=user_question,
        )

        # Skills are now built by PromptBuilder._build_skills_content()
        # (included in DEFAULT_PROMPT_ORDER as "skills" component).

        return messages

    async def _generate_summary(self) -> None:
        """Generate discussion summary — delegates to run_loop module."""
        from nanobot.groupchat.orchestra.run_loop import generate_summary
        await generate_summary(self)

    async def _run_loop(self) -> None:
        """Main group chat loop — delegates to run_loop module."""
        from nanobot.groupchat.orchestra.run_loop import run_loop
        await run_loop(self)


"""Direct 1-on-1 chat mode for group chat engine.

Handles single-agent conversations when exactly one agent is active.
Manages session setup, message building, streaming, and response handling.

Supports user interjection: after the agent replies, the loop waits
briefly for new user messages (via engine._direct_chat_queue).  If the
user "interrupts" before the agent finishes or sends a follow-up right
after, the agent immediately continues with the new context — similar
to how broadcast mode's _user_listener works.
"""


import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.groupchat.display.streaming import StreamingDisplay
from nanobot.utils.helpers import cn_now as _cn_now


# Maximum follow-up cycles (safety cap to prevent infinite loops)
_MAX_CYCLES = 999  # effectively unlimited


async def direct_chat(engine: Any, user_message: str) -> str | None:
    """Send message to single active agent (1-on-1 mode) with interjection.

    Runs the first reply, then loops waiting for user interjections
    via ``engine._direct_chat_queue``.  Exits when no interjection
    arrives within the timeout window.

    Args:
        engine: GroupChatEngine instance.
        user_message: The user's message text.

    Returns:
        Response text to send (or None if already sent via streaming).
    """
    if len(engine._active_agents) != 1:
        return None

    agent_name = engine._active_agents[0]
    agent = engine.registry[agent_name]

    # Ensure session directory exists
    if not engine._session_dir:
        from nanobot.utils.helpers import cn_now as _cn_now_local
        timestamp = _cn_now_local().strftime("%Y%m%d-%H%M%S")
        sessions_dir = Path.home() / ".nanobot" / "collab-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        engine._session_dir = sessions_dir / f"gc-{timestamp}"
        engine._session_dir.mkdir(parents=True, exist_ok=True)
        engine._save_event("session_start", extra={
            "agents": [agent_name],
            "mode": "direct",
            "topic": engine._topic or "",
            "leader": None,
            "models": {agent_name: agent.get("model", "?")},
        })

    # Build messages via PromptBuilder (unified prompt construction)
    messages: list[dict[str, Any]] = engine.prompt_builder.build_agent_prompt(
        agent_name,
        registry=engine.registry,
        active_agents=[agent_name],
        history=engine._history,
        leader=None,
        round_num=0,
    )
    messages.append({"role": "user", "content": user_message})

    # ── Cycle loop: reply → wait for interjection → reply again ──
    cycle = 0
    current_user_msg = user_message
    last_response: str | None = None

    while cycle < _MAX_CYCLES:
        cycle += 1

        # ── Streaming setup ──
        _header = f"💬 {agent_name}:\n\n"
        stream = StreamingDisplay(_header, engine._send_and_get_id_fn, engine._edit_fn)
        _delta_cb = stream.on_delta if stream.enabled else None
        _reset_cb = stream.on_reset if stream.enabled else None

        # Log full context before LLM call
        _dc_total_chars = sum(
            len(m.get("content", "")) if isinstance(m.get("content"), str)
            else sum(len(b.get("text", "")) for b in m.get("content", []) if isinstance(b, dict))
            if isinstance(m.get("content"), list) else 0
            for m in messages
        )
        logger.info(
            "direct_chat [{}] cycle {} start: msgs={} total_chars={} user_msg={}",
            agent_name, cycle, len(messages), _dc_total_chars, current_user_msg,
        )

        try:
            content, tools_used, stats = await engine._chat_with_tools(
                messages=messages,
                model=agent["model"],
                agent_name=agent_name,
                is_direct=True,
                on_content_delta=_delta_cb,
                on_content_reset=_reset_cb,
            )
            log_request(engine, agent_name, agent["model"], "direct",
                        reply_len=len(content), msgs=len(messages),
                        tools=tools_used,
                        input_preview=current_user_msg, output=content,
                        **stats)
            logger.info(
                "direct_chat [{}] cycle {} result: content={}",
                agent_name, cycle, content,
            )
            _tool_details = stats.get("tool_calls_detail", [])
            if content or _tool_details:
                engine._add_message("用户", current_user_msg)
                # Store content with tool call log so model sees its history
                history_content = (content or "") + build_tool_log(_tool_details)
                engine._add_message(agent_name, history_content)
                # Bug 7 fix: run history compression in single-agent mode too
                await engine._maybe_compress_history()
                # Append token usage to displayed reply
                tok = stats.get("tokens", {})
                total = tok.get("total", 0)
                display_content = content or "[仅调用了工具，无文字回复]"
                if total > 0:
                    p, c = tok.get("prompt", 0), tok.get("completion", 0)
                    cost = stats.get("cost", 0) or 0
                    cache_t = stats.get("cache_tokens", 0) or 0
                    reasoning_t = (stats.get("provider_meta") or {}).get("reasoning_tokens", 0) or 0
                    stat_line = _d.format_token_stats(p, c, cost=cost, cache_tokens=cache_t, reasoning_tokens=reasoning_t)
                    display_content = f"{display_content}\n\n{stat_line}"
                await stream.finalize(display_content, fallback_send=engine._send)
                last_response = content or ""
            else:
                if stream.msg_id and engine._edit_fn:
                    try:
                        await engine._edit_fn(stream.msg_id, f"⚠️ {agent_name} 返回空回复")
                    except Exception:
                        pass
                else:
                    await engine._send(f"⚠️ {agent_name} 返回空回复 (模型可能暂时异常，请重试)")
                break  # empty reply — stop cycling

        except Exception as e:
            logger.error("Direct chat with {} failed: {}", agent_name, e)
            log_request(engine, agent_name, agent["model"], "direct",
                        msgs=len(messages),
                        error=str(e))
            await engine._send(f"⚠️ {agent_name} 回复失败: {e}")
            break

        # ── Wait for user interjection ──
        # Drain the queue: pick up any messages queued while agent was talking
        try:
            new_msg = await asyncio.wait_for(
                engine._direct_chat_queue.get(), timeout=1.0,
            )
        except asyncio.TimeoutError:
            # No interjection — normal exit
            break

        # Got an interjection! Log and continue the cycle.
        logger.info("Direct chat: interjection received ({} chars), cycle {}", len(new_msg), cycle)
        await engine._send(f"── 插话 ──")

        current_user_msg = new_msg

        # Inject the agent's previous reply and the new user message
        # into the messages list so the agent sees full context
        if content:
            messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": new_msg})

    # Return None — all output already sent via streaming/send
    return None


"""Tool-augmented chat for group chat agents.

Extracts the tool calling loop from ``engine.py``, including:
- Tool callback factories (on_tool_start / on_tool_result display)
- Message snapshot for structured logging
- Stats packaging from tool_loop result
"""


from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.display import display as _d


# ── Helpers ──────────────────────────────────────────────────

def snapshot_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a full snapshot of messages for logging (before tool_loop mutates them)."""
    snap: list[dict[str, Any]] = []
    for m in messages:
        entry: dict[str, Any] = {"role": m.get("role", "?")}
        if m.get("name"):
            entry["name"] = m["name"]
        content = m.get("content", "")
        if isinstance(content, str):
            entry["content"] = content
            entry["content_len"] = len(content)
        elif isinstance(content, list):
            text_parts = [b.get("text", "") for b in content if isinstance(b, dict)]
            joined = " ".join(text_parts)
            entry["content"] = joined
            entry["content_len"] = len(joined)
        else:
            entry["content"] = str(content) if content else ""
            entry["content_len"] = len(str(content)) if content else 0
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        snap.append(entry)
    return snap


def build_stats(result: Any, tool_defs: list | None, tool_names: list[str],
                messages_snapshot: list[dict], sampling: dict, max_tokens: int) -> dict[str, Any]:
    """Package tool_loop result into a stats dict."""
    return {
        "iterations": result.iterations,
        "latency": result.latency,
        "tokens": result.token_usage,
        "calls": result.call_details,
        "tool_calls_detail": result.tool_calls_detail,
        "tools_available": result.tools_available,
        "tool_defs_count": len(tool_defs) if tool_defs else 0,
        "tool_names": tool_names,
        "messages_snapshot": messages_snapshot,
        "sampling_params": sampling,
        "max_tokens": max_tokens,
        "status_code": result.status_code,
        "finish_reason": result.finish_reason,
        "cost": result.cost,
        "cache_tokens": result.cache_tokens,
        "provider_meta": result.provider_meta,
    }


# ── Tool callback factory ───────────────────────────────────

def make_tool_callbacks(
    agent_name: str,
    save_event: Callable,
    send_fn: Callable[[str], Awaitable[None]] | None,
    send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None,
    edit_fn: Callable[[int, str], Awaitable[None]] | None,
    iter_usage_ref: dict | None = None,
) -> tuple[Callable, Callable]:
    """Create on_tool_start / on_tool_result callbacks for an agent.

    Supports parallel tool calls by keying state on tool_call_id rather
    than using a single shared variable pair.

    Args:
        iter_usage_ref: Shared mutable dict updated with per-iteration token
            usage before tool execution. When provided, a token suffix is
            appended to the tool result message.

    Returns:
        (on_tool_start, on_tool_result) async callbacks.
    """
    # tool_call_id → (msg_id, original_text)
    _pending_tools: dict[str, tuple[int | None, str]] = {}
    _temp_counter = 0

    async def on_tool_start(
        name: str,
        args: dict,
        tool_call_id: str | None = None,
    ) -> None:
        nonlocal _temp_counter
        if not isinstance(args, dict):
            args = {}
        # Persist tool_call event
        save_event("tool_call", agent=agent_name, extra={
            "tool": name,
            "args": dict(args),
        })
        # Full logging to server log
        import json as _json_tc
        logger.info(
            "tool_chat [{}] tool_call: {}({})",
            agent_name, name, _json_tc.dumps(args, ensure_ascii=False),
        )
        short = (
            args.get("command") or args.get("query")
            or args.get("url") or args.get("path") or ""
        )
        if not short and args:
            short = list(args.values())[0]
        if isinstance(short, str) and len(short) > 80:
            short = short[:80] + "…"
        text = _d.tool_call_line(agent_name, name, short if isinstance(short, str) else str(short))

        # Generate a fallback key when tool_loop doesn't pass tool_call_id
        if tool_call_id is None:
            _temp_counter += 1
            tool_call_id = f"_temp-{agent_name}-{_temp_counter}"

        msg_id: int | None = None
        if send_and_get_id_fn:
            msg_id = await send_and_get_id_fn(text)
        elif send_fn:
            await send_fn(text)

        _pending_tools[tool_call_id] = (msg_id, text)

    async def on_tool_result(name: str, tool_call_id: str, result: str) -> None:
        save_event("tool_result", agent=agent_name, extra={
            "tool": name,
            "result_len": len(result) if result else 0,
            "success": not (result or "").startswith("Error:"),
        })
        # Full result logging to server log
        logger.info(
            "tool_chat [{}] tool_result: {} ({}c): {}",
            agent_name, name, len(result) if result else 0, result,
        )
        if not result:
            _pending_tools.pop(tool_call_id, None)
            return
        rlen = len(result)
        preview = result.strip().replace("\n", " ")[:60]
        result_line = f"↳ {preview}{'…' if rlen > 60 else ''} ({rlen:,}字)"

        # Build token suffix from per-iteration usage if available
        token_suffix = ""
        if iter_usage_ref:
            u = iter_usage_ref
            p = u.get("prompt_tokens", 0)
            c = u.get("completion_tokens", 0)
            total = u.get("total_tokens", 0) or (p + c)
            cost = u.get("cost")
            cache_t = u.get("cache_tokens", 0) or u.get("cache_read_input_tokens", 0)
            if total:
                token_suffix = "\n" + _d.format_token_stats(p, c, cost=cost, cache_tokens=cache_t)

        pending = _pending_tools.pop(tool_call_id, None)
        if pending and pending[0] is not None and edit_fn and pending[1]:
            try:
                updated = f"{pending[1]}\n{result_line}{token_suffix}"
                await edit_fn(pending[0], updated)
            except Exception as exc:
                logger.warning("tool_chat [{}] edit_fn failed: {}", agent_name, exc)
                if send_fn:
                    await send_fn(result_line + token_suffix)
        elif send_fn:
            await send_fn(result_line + token_suffix)

    return on_tool_start, on_tool_result


# ── Main function ────────────────────────────────────────────

async def chat_with_tools(
    *,
    provider: Any,
    messages: list[dict[str, Any]],
    model: str,
    agent_name: str,
    tool_registry: Any,
    tool_defs: list | None,
    max_tokens: int,
    max_iterations: int = 5,
    session_id: str = "direct",
    agent_sampling: dict[str, Any] | None = None,
    is_direct: bool = False,
    debug_context: bool = False,
    topic: str = "",
    clean_response: Callable[[str], str] | None = None,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_content_reset: Callable[[], Awaitable[None]] | None = None,
    on_tool_start_override: Callable | None = None,
    on_tool_result_override: Callable | None = None,
    save_event: Callable | None = None,
    send_fn: Callable[[str], Awaitable[None]] | None = None,
    send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None = None,
    edit_fn: Callable[[int, str], Awaitable[None]] | None = None,
    force_no_tools: bool = False,
) -> tuple[str, list[str], dict[str, Any]]:
    """Run tool-augmented chat loop. Standalone version of engine._chat_with_tools.

    Returns:
        (content, tools_used, stats)
    """
    from nanobot.groupchat.orchestra.tools.tool_loop import tool_loop

    # Langfuse trace metadata
    trace_metadata = {
        "trace_name": f"{'direct' if is_direct else 'group'}_{agent_name}",
        "trace_user_id": "groupchat",
        "tags": [agent_name, "direct" if is_direct else "group"],
        "generation_name": f"{agent_name}_loop",
        "debug_context": debug_context,
        "log_agent": agent_name,
        "log_session": session_id,
        "log_topic": topic,
        "log_mode": "direct" if is_direct else "group",
    }

    # Default tool callbacks
    _save_event = save_event or (lambda *a, **kw: None)
    # Shared mutable dict updated with per-iteration token usage so that
    # on_tool_result can append a token suffix to the tool call message.
    _iter_usage_ref: dict = {}
    default_start, default_result = make_tool_callbacks(
        agent_name, _save_event, send_fn, send_and_get_id_fn, edit_fn,
        iter_usage_ref=_iter_usage_ref,
    )

    # Load configurable result_max_chars for direct mode
    try:
        from nanobot.groupchat.history.history_settings import direct_result_max_chars
        _direct_result_max = direct_result_max_chars()
    except Exception:
        _direct_result_max = 8_000

    effective_defs = None if force_no_tools else tool_defs

    # Compute context stats for logging
    def _calc_total_chars(msgs: list[dict]) -> int:
        total = 0
        for m in msgs:
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += len(block.get("text", ""))
        return total

    _total_chars = _calc_total_chars(messages)
    logger.info(
        "chat_with_tools: agent={} model={} tool_defs={} is_direct={} msgs={} total_chars={}",
        agent_name, model, len(tool_defs) if tool_defs else 0, is_direct,
        len(messages), _total_chars,
    )

    # Snapshot messages before tool_loop mutates them
    messages_snap = snapshot_messages(messages)

    # Use the (caller-provided) per-agent hyperparams override if present.
    # Note: the caller (_chat_with_tools) performs a fresh read from the live
    # engine.registry right before invoking us, so /editagent changes are visible
    # without restart. The passed agent_sampling is the effective merged value
    # for this turn.
    base = getattr(provider, "sampling_params", {}) or {}
    sampling = dict(base)  # copy to avoid mutating caller's base unintentionally
    if agent_sampling:
        sampling.update(agent_sampling)  # agent overrides win
        logger.info("chat_with_tools: agent={} merged hyperparams: {}", agent_name, list(agent_sampling.keys()))

    _orig_sampling = getattr(provider, "sampling_params", None)
    if _orig_sampling is not None:
        provider.sampling_params = sampling  # temporary per-turn override
    tool_names = [d.get("function", {}).get("name", "?") for d in (tool_defs or [])]

    # Per-iteration token usage callback — update shared ref so tool callbacks
    # can show a token suffix on each tool call message.
    async def _on_iter_usage(usage: dict) -> None:
        _iter_usage_ref.clear()
        _iter_usage_ref.update(usage)

    try:
        result = await tool_loop(
            provider=provider,
            messages=messages,
            tool_registry=tool_registry,
            model=model,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
            tool_defs=effective_defs,
            metadata=trace_metadata,
            reasoning_effort=sampling.get("reasoning_effort") if sampling else None,
            on_tool_start=on_tool_start_override or default_start,
            on_tool_result=on_tool_result_override or default_result,
            on_iteration_usage=_on_iter_usage,
            on_content_delta=on_content_delta,
            on_content_reset=on_content_reset,
            clean_response=clean_response,
            result_max_chars=_direct_result_max,
        )
    finally:
        # Restore original sampling params to avoid leaking between agents.
        # Use a copy of the saved original in case the provider object was
        # further mutated during the turn.
        if _orig_sampling is not None:
            provider.sampling_params = dict(_orig_sampling) if isinstance(_orig_sampling, dict) else _orig_sampling

    content = result.content or ""
    # Use effective_defs (not original tool_defs) for accurate stats
    # when force_no_tools=True
    stats = build_stats(result, effective_defs, tool_names, messages_snap, sampling, max_tokens)

    # Log complete result
    logger.info(
        "chat_with_tools result: agent={} iters={} latency={:.2f}s "
        "tokens={} tools_used={} finish={} content={}",
        agent_name, result.iterations, result.latency,
        result.token_usage, result.tools_used, result.finish_reason, content,
    )

    return content, result.tools_used, stats


"""Shared utilities for the groupchat package."""


from datetime import datetime, timezone, timedelta
from typing import Any




def build_tool_log(tool_calls_detail: list[dict[str, Any]]) -> str:
    """Build a tool call summary for conversation history.

    Appended to the assistant's content so the model can see what tools
    it previously called on the next turn.  Preview lengths vary by tool
    type — search/fetch results get longer previews (the model needs to
    remember *what* it found), while exec/chatroom keep it shorter.

    Total output is capped at ~4000 chars to prevent context bloat.

    Uses <previous_tool_calls> wrapper (instead of the old [工具调用记录])
    so weak/cheap models are much less likely to hallucinate by imitating
    the marker in their output (e.g. starting a reply with "我刚刚搜索了一些
    【工具调用记录】search->114字" when no tool was actually called).

    Returns empty string if no tool calls were made.
    """
    if not tool_calls_detail:
        return ""

    # Per-tool preview length limits
    _PREVIEW_LIMITS = {
        "web_search": 1500,
        "web_fetch": 1500,
        "read_file": 800,
        "exec": 500,
        "list_dir": 300,
        "chatroom_send": 200,
        "wait": 200,
        "write_file": 100,
        "edit_file": 100,
    }
    _DEFAULT_PREVIEW = 500
    _TOTAL_CAP = 4000

    lines: list[str] = []
    total_chars = 0

    for tc in tool_calls_detail:
        name = tc.get("name", "?")
        args_raw = tc.get("args", "")
        result_len = tc.get("result_len", 0)
        preview = tc.get("result_preview", "")
        success = tc.get("success", True)
        is_dup = tc.get("duplicate", False)

        # memory_palace store with visible=false: suppress preview in chat log
        _mp_hidden = False
        if name == "memory_palace":
            try:
                _mp_args = __import__("json").loads(args_raw) if isinstance(args_raw, str) else args_raw
            except Exception:
                _mp_args = {}
            if _mp_args.get("action") == "store" and _mp_args.get("visible") is False:
                _mp_hidden = True

        # Extract the key argument (query / url / command / path) for display
        try:
            args_dict = __import__("json").loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args_dict = {}
        key_arg = (
            args_dict.get("query")
            or args_dict.get("command")
            or args_dict.get("url")
            or args_dict.get("path")
            or args_dict.get("message", "")[:80]
            or ""
        )
        if isinstance(key_arg, str) and len(key_arg) > 80:
            key_arg = key_arg[:77] + "..."

        # Build result info with tool-appropriate preview length
        max_preview = _PREVIEW_LIMITS.get(name, _DEFAULT_PREVIEW)

        if is_dup:
            result_info = "[重复调用,已跳过]"
        elif _mp_hidden:
            result_info = "✅ 已存储 (内容已隐藏)"
        elif not success:
            err = tc.get("error", "")
            result_info = f"[失败: {err[:80]}]"
        elif name in ("chatroom_send", "wait", "write_file", "edit_file"):
            # For communication/write tools, just show status
            result_info = f"({result_len:,}字)" if result_len else "OK"
        elif isinstance(preview, str) and preview:
            # Remaining budget check
            remaining = _TOTAL_CAP - total_chars
            effective_limit = min(max_preview, remaining)
            if effective_limit < 50:
                result_info = f"({result_len:,}字)"
            else:
                short = preview.strip()[:effective_limit]
                truncated = len(preview) > effective_limit
                result_info = f"{short}{'…' if truncated else ''} ({result_len:,}字)"
        else:
            result_info = f"({result_len:,}字)"

        line = f"• {name}({key_arg}) → {result_info}"
        lines.append(line)
        total_chars += len(line)

        # Hard cap: stop adding more details
        if total_chars >= _TOTAL_CAP:
            remaining_count = len(tool_calls_detail) - len(lines)
            if remaining_count > 0:
                lines.append(f"  (还有 {remaining_count} 个工具调用，已省略)")
            break

    if not lines:
        return ""

    # Use XML-style internal tag so weak models are less likely to imitate it
    # as natural output. The model is explicitly instructed not to reproduce it.
    return "\n\n<previous_tool_calls>\n" + "\n".join(lines) + "\n</previous_tool_calls>\n"


def log_request(
    engine: Any,
    agent: str,
    model: str,
    mode: str,
    reply_len: int = 0,
    **extra: Any,
) -> None:
    """Append a structured entry to engine._request_log.

    Centralizes the common request logging pattern used by speaker,
    direct_chat, broadcast, and orchestra modules.
    """
    entry: dict[str, Any] = {
        "agent": agent,
        "model": model,
        "reply_len": reply_len,
        "time": _cn_now().strftime("%H:%M:%S"),
        "mode": mode,
    }
    entry.update(extra)
    engine._request_log.append(entry)
    # Cap to prevent unbounded memory growth
    if len(engine._request_log) > 1000:
        engine._request_log = engine._request_log[-500:]


