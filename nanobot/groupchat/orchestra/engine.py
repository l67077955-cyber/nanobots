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
from nanobot.groupchat.orchestra.agent_runner import AgentRunner
from nanobot.groupchat.orchestra.conversation_context import ConversationContext
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
        """Register chatroom tools and warm the default workspace registry cache."""
        from nanobot.groupchat.orchestra.tools.chatroom_tools import ChatroomSendTool, WaitTool

        self._chatroom_send_tool = ChatroomSendTool(mailbox=self._mailbox)
        self._wait_tool = WaitTool(mailbox=self._mailbox)
        self._tool_registry_cache: dict[str, "ToolRegistry"] = {}
        self.tools = self._ensure_tool_registry(
            self.workspace, include_chatroom=True,
        )
        logger.info("Groupchat: warmed default tool registry ({} tools)", len(self.tools))

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

    def _registry_cache_key(self, ws: Path, *, include_chatroom: bool) -> str:
        suffix = "g" if include_chatroom else "d"
        return f"{ws}:{suffix}"

    def _ensure_tool_registry(
        self,
        ws: Path,
        *,
        include_chatroom: bool,
        agent_name: str | None = None,
    ) -> "ToolRegistry":
        """Get or build a cached ToolRegistry for a workspace scope."""
        key = self._registry_cache_key(ws, include_chatroom=include_chatroom)
        if key not in self._tool_registry_cache:
            reg = self._build_tool_registry(ws)
            if include_chatroom:
                reg.register(self._chatroom_send_tool)
                reg.register(self._wait_tool)
            self._tool_registry_cache[key] = reg
            logger.info(
                "Groupchat: built tool registry for {} (chatroom={})",
                ws, include_chatroom,
            )
        reg = self._tool_registry_cache[key]
        if agent_name:
            self._sync_forget_tool(reg, agent_name)
        return reg

    def _get_agent_registry(self, agent_name: str) -> "ToolRegistry":
        """Get (or build and cache) the group-mode registry for an agent."""
        ws = self._resolve_agent_workspace(agent_name)
        return self._ensure_tool_registry(
            ws, include_chatroom=True, agent_name=agent_name,
        )

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers and inject tools into all registries (lazy, one-time)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            seen: set[int] = set()
            for reg in self._tool_registry_cache.values():
                if id(reg) in seen:
                    continue
                seen.add(id(reg))
                await connect_mcp_servers(self._mcp_servers, reg, self._mcp_stack)
            self._mcp_connected = True
            logger.info(
                "GroupChat MCP: connected {} server(s), tools injected into {} registries",
                len(self._mcp_servers), len(seen),
            )
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
        # ConversationContext: the single mutation seam for history. All
        # add/clear/compress goes through here; ``self._history`` below is a
        # READ-ONLY view of ``self.history.messages`` (Step 1 coupling refactor).
        self._context = ConversationContext(self.history, self._state)

        # Runtime state (ephemeral, not persisted)
        self._task: asyncio.Task | None = None
        self._running = False
        # Broadcast round: per-agent tasks registered by broadcast_round()
        # so remove_agent() can cancel an in-flight agent mid-round.
        self._broadcast_tasks: dict[str, asyncio.Task] = {}
        # Per-agent runtime facades (cancel signal + state) for the current
        # round. Populated by broadcast _run_one; the canonical handle new code
        # should use instead of mailbox._busy_agents / _interrupt_events.
        self._runners: dict[str, AgentRunner] = {}
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
        self._view_channel: str | None = None
        self._view_chat_id: str | None = None
        # Pinned outbound route for in-flight direct chat (survives dashboard stomp)
        self._reply_channel: str | None = None
        self._reply_chat_id: str | None = None
        self._stream_chat_id: str | None = None

        # Direct chat interjection state (single-agent mode)
        self._direct_chat_task: asyncio.Task | None = None
        self._direct_chat_queue: asyncio.Queue[tuple[str, list[str] | None]] = asyncio.Queue()
        self._active_stream: Any | None = None
        self.stream_replies: bool = True  # set from config.channels.send_progress at gateway startup

        # Restore persisted chat state last so defaults above do not clobber it.
        self._restore_chat_state()

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

    @property
    def room_id(self) -> str:
        from nanobot.groupchat.room_observability import resolve_room_id
        return resolve_room_id(self._view_channel, self._view_chat_id)

    def pin_reply_route(self, channel: str, chat_id: str) -> None:
        """Pin outbound delivery for an in-flight user turn."""
        self._reply_channel = channel
        self._reply_chat_id = chat_id
        self.set_tool_context(channel, chat_id)

    def clear_reply_route(self) -> None:
        """Release pinned outbound route after direct chat completes."""
        self._reply_channel = None
        self._reply_chat_id = None

    def set_tool_context(self, channel: str, chat_id: str) -> None:
        """Set channel/chat context for cron CLI script via env vars.

        Called when a channel wires routing context (channel + chat_id).
        """
        import os
        if (
            self._reply_channel
            and self._direct_chat_task
            and not self._direct_chat_task.done()
            and (channel, chat_id) != (self._reply_channel, self._reply_chat_id)
        ):
            return
        self._view_channel = channel
        self._view_chat_id = chat_id
        os.environ["NANOBOT_CHANNEL"] = channel
        os.environ["NANOBOT_CHAT_ID"] = chat_id
        # Also update MessageTool context in all cached registries so agents
        # can send files (PDFs, images) to the correct Telegram chat.
        self._update_message_tool_context(channel, chat_id)

    def _update_message_tool_context(self, channel: str, chat_id: str) -> None:
        """Push channel/chat_id into all MessageTool instances in the registry cache."""
        from nanobot.tools.message import MessageTool
        for reg in getattr(self, "_tool_registry_cache", {}).values():
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
    def has_outbound(self) -> bool:
        """Whether outbound delivery is wired (bus or legacy send_fn)."""
        return self.has_send_fn or bool(
            self._send_outbound_fn and self._view_channel and self._view_chat_id,
        )

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

    # ── History access (Step 1 coupling refactor) ─────────────────────────
    # ``_history`` is now a READ-ONLY view of the live messages list. All
    # mutation goes through ``self._context`` (ConversationContext) / the
    # ``self.history`` (HistoryContext) it wraps. The scattered
    # ``self._history = self.history.messages`` re-syncs are gone — a property
    # always reflects the current list even after HistoryContext rebuilds it.
    @property
    def _history(self) -> list[dict[str, str]]:
        return self.history.messages

    @property
    def context(self) -> ConversationContext:
        """The single mutation seam for conversation history."""
        return self._context

    def clear_history(self) -> None:
        """Clear conversation history and request log, but keep active agents and loop running."""
        self.interrupt_active_turn()
        self.history.clear()
        self._request_log.clear()
        state = getattr(self, "_state", None)
        if state is not None:
            state.clear_history_snapshot()
        self._round = 0
        self._topic = ""

    def reset(self) -> None:
        """Clear history, request log, active agents, and stop the loop."""
        self.stop()
        self.history.clear()
        self._request_log.clear()
        self._active_agents.clear()
        state = getattr(self, "_state", None)
        if state is not None:
            state.clear_history_snapshot()
        self._round = 0
        self._topic = ""

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

        was_single = len(self._active_agents) == 1
        self._active_agents.append(matched)
        if was_single and len(self._active_agents) >= 2:
            # End in-flight 1-on-1 turn before switching to group mode.
            self.interrupt_active_turn()
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

    @property
    def mailbox(self) -> MailboxHub:
        return self._mailbox

    def refresh_interrupt_ranks(self) -> None:
        """Recompute mailbox interrupt hierarchy from live registry ranks."""
        if not self.is_running:
            return
        ranks_map: dict[str, str] = {}
        for ag in self.active_agents:
            cfg = self.registry.get(ag, {})
            ranks_map[ag] = str(cfg["rank"]) if "rank" in cfg else "basic"
        self._mailbox.set_ranks(ranks_map, leader=self.leader or "")

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

    # ── Tool-augmented chat ───

    def get_agent_enabled_tool_names(self, agent_name: str) -> list[str]:
        """Get the names of tools currently enabled for an agent."""
        agent_cfg = self.registry.get(agent_name, {})
        # Session override takes precedence
        if hasattr(self, "_session_tools_override") and agent_name in self._session_tools_override:
            tools_cfg = self._session_tools_override[agent_name]
        else:
            tools_cfg = agent_cfg.get("tools")
            
        if isinstance(tools_cfg, dict):
            names = [k for k, v in tools_cfg.items() if v]
            from nanobot.groupchat.tool_policy import forget_tool_enabled
            if forget_tool_enabled(agent_cfg, session_override=tools_cfg) and "forget" not in names:
                names.append("forget")
            return names
        elif agent_cfg.get("tools_enabled", False) or agent_cfg.get("_default"):
            return list(self.TOOL_NAMES)

        from nanobot.groupchat.tool_policy import forget_tool_enabled
        return ["forget"] if forget_tool_enabled(agent_cfg) else []

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
            from nanobot.groupchat.tool_policy import forget_tool_enabled
            if forget_tool_enabled(agent_cfg, session_override=tools_cfg):
                enabled.add("forget")
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

        from nanobot.groupchat.tool_policy import forget_tool_enabled
        if forget_tool_enabled(agent_cfg):
            return [
                d for d in registry.get_definitions()
                if d.get("function", {}).get("name") == "forget"
            ]

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

        from nanobot.groupchat.orchestra.tools.tool_chat import (
            chat_with_tools,
            resolve_max_tool_iterations,
        )

        # Tool selection — use per-agent registry based on workspace_scope
        agent_cfg = self.registry.get(agent_name, {})
        ws = self._resolve_agent_workspace(agent_name)
        tool_registry = self._ensure_tool_registry(
            ws, include_chatroom=not is_direct, agent_name=agent_name,
        )
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

        if max_iterations == 5:
            max_iterations = resolve_max_tool_iterations(self, agent_name, is_direct=is_direct)

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

    def direct_chat_inject(
        self,
        user_message: str,
        *,
        media: list[str] | None = None,
    ) -> bool:
        """Inject a user interjection into an in-progress direct chat.

        Returns True if injected, False if no direct chat is running.
        """
        if self._direct_chat_task and not self._direct_chat_task.done():
            self._direct_chat_queue.put_nowait((user_message, media))
            logger.info(
                "Direct chat: interjection queued ({} chars, {} media)",
                len(user_message), len(media or []),
            )
            return True
        return False

    async def direct_chat(
        self,
        user_message: str,
        *,
        media: list[str] | None = None,
    ) -> str | None:
        """Send message to single active agent — delegates to direct_chat module."""
        from nanobot.groupchat.orchestra.direct_chat import direct_chat as _direct_chat
        return await _direct_chat(self, user_message, media=media)

    def inject(
        self,
        message: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        """Inject a user message into the shared chat loop.

        The loop handles both one-agent and multi-agent conversations.  Keep
        this path unified so single chat gets the same interrupt/mailbox
        semantics as group chat.
        """
        from nanobot.groupchat.room_observability import emit_room_event
        emit_room_event(
            room_id=self.room_id,
            kind="user_input",
            source="inject",
            agent="User",
            content=message,
            extra={
                "active_agents": list(self._active_agents),
                "media_count": len(media or []),
            },
        )
        n = len(self._active_agents)
        if n == 0:
            return
        self.interrupt_active_turn()
        if not self._running:
            self._start_group_loop()
        if self._running:
            self._input_queue.put_nowait(message)

    def request_summary(self) -> None:
        """Request a discussion summary."""
        if self._running:
            self._input_queue.put_nowait("__SUMMARY__")

    def stop(self) -> None:
        """Stop the group loop and any in-flight direct chat; keep active agents."""
        self._stop_group_loop()
        self.interrupt_active_turn(reason="⏹ 已停止")
        try:
            asyncio.ensure_future(self._disconnect_mcp())
        except RuntimeError:
            pass  # no event loop — shutdown scenario

    # ── Internal ─────────────────────────────────────────────

    def register_active_stream(self, stream: Any) -> None:
        """Track the in-flight StreamingDisplay for command interrupts."""
        self._active_stream = stream

    def clear_active_stream(self, stream: Any) -> None:
        if self._active_stream is stream:
            self._active_stream = None

    def _abort_active_stream_sync(self, reason: str = "⏹ 已中断") -> None:
        """Best-effort stream cleanup from sync code (/commands, inject, etc.)."""
        stream = getattr(self, "_active_stream", None)
        if stream is None:
            return
        self._active_stream = None
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(stream.abort(reason=reason))
        except RuntimeError:
            pass

    def interrupt_active_turn(self, *, reason: str = "⏹ 已中断") -> None:
        """Abort streaming UI and cancel in-flight direct chat (not full /stop)."""
        self._abort_active_stream_sync(reason)
        self._cancel_direct_chat()

    def _cancel_direct_chat(self) -> None:
        """Cancel an in-flight direct chat task, if any."""
        task = self._direct_chat_task
        if task and not task.done():
            task.cancel()
        self._direct_chat_task = None

    async def _run_direct_chat(
        self,
        message: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        """Run direct_chat as a tracked background task (for inject routing)."""
        try:
            await self.direct_chat(message, media=media)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Direct chat task failed: {}", e)
        finally:
            self.clear_reply_route()
            if self._direct_chat_task is asyncio.current_task():
                self._direct_chat_task = None

    def _resolve_agent_name(self, name: str) -> str | None:
        """Case-insensitive agent name resolution."""
        for reg_name in self.registry:
            if reg_name.lower() == name.lower():
                return reg_name
        return None

    def _start_group_loop(self) -> None:
        """Start the async group chat loop (2+ agents)."""
        self.interrupt_active_turn()
        # Always cancel any prior task to avoid duplicate loops
        if self._task and not self._task.done():
            self._task.cancel()
        if self._running:
            return
        self._running = True
        self._input_queue = asyncio.Queue()
        if not self._topic:
            self._topic = "自由讨论"

        self._ensure_session_dir("broadcast")

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
        # Drop runner facades too — their tasks are gone.
        self._runners.clear()
        # Finalize session: write session_end + session_summary.json
        if self._session_dir:
            self._state.close_session(topic=self._topic)
            self._session_dir = None

    def runner(self, name: str) -> AgentRunner | None:
        """Runtime facade for an agent active in the current round.

        New code should call this (and the runner's interrupt/cancel API)
        instead of reaching into mailbox._busy_agents / _interrupt_events.
        Returns None if the agent has no runner this round.
        """
        return self._runners.get(name)

    @property
    def runners(self) -> dict[str, AgentRunner]:
        """All runners for the currently-running round."""
        return self._runners

    def _ensure_session_dir(self, mode: str, *, agent_name: str | None = None) -> None:
        """Create collab session directory and log session_start (idempotent)."""
        if self._session_dir:
            return
        timestamp = _cn_now().strftime("%Y%m%d-%H%M%S")
        sessions_dir = Path.home() / ".nanobot" / "collab-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = sessions_dir / f"gc-{timestamp}"
        self._session_dir.mkdir(parents=True, exist_ok=True)
        if mode == "direct":
            agents = [agent_name] if agent_name else []
            models = (
                {agent_name: self.registry.get(agent_name, {}).get("model", "?")}
                if agent_name else {}
            )
            extra = {
                "agents": agents,
                "mode": "direct",
                "topic": self._topic or "",
                "leader": None,
                "models": models,
            }
        else:
            extra = {
                "agents": list(self._active_agents),
                "mode": "broadcast",
                "topic": self._topic,
                "leader": self._leader,
                "models": {
                    n: self.registry.get(n, {}).get("model", "?")
                    for n in self._active_agents
                },
            }
        self._save_event("session_start", extra=extra)
        self._state.save_current_session(
            self._session_dir,
            topic=self._topic,
            round_num=self._round,
            agents=list(self._active_agents) if mode != "direct" else extra.get("agents", []),
            leader=self._leader if mode != "direct" else None,
        )

    async def _send(self, text: str, progress: bool = False) -> None:
        from nanobot.bus.events import OutboundMessage
        from nanobot.groupchat.room_observability import emit_room_event
        emit_room_event(
            room_id=self.room_id,
            kind="ui_push",
            source="telegram_view",
            content=text,
            extra={"chars": len(text)},
        )
        try:
            channel = self._reply_channel or self._view_channel
            chat_id = self._reply_chat_id or self._view_chat_id
            if channel and chat_id and self._send_outbound_fn:
                await self._send_outbound_fn(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=text,
                    metadata={"_progress": progress} if progress else {},
                ))
            elif self._send_fn:
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

    def _persist_chat_state(self) -> None:
        """Write live history to disk so gateway restarts do not lose context."""
        if not self._history:
            return
        self._state.save_history_snapshot(
            history=self._history,
            topic=self._topic,
            round_num=self._round,
            session_dir=self._session_dir,
        )

    def _restore_chat_state(self) -> None:
        """Reload chat history from the last persisted snapshot (gateway restart)."""
        snapshot = self._state.load_history_snapshot()
        if not snapshot:
            return
        restored = []
        for m in snapshot.get("history", []):
            if not (isinstance(m, dict) and m.get("content")):
                continue
            item = {"sender": m.get("sender", "?"), "content": m.get("content", "")}
            # Preserve the structured compact-boundary flag across restart so
            # is_compact_summary() recognises prior summary blocks without
            # relying on the legacy string-prefix fallback. Other extra keys
            # are intentionally dropped to keep the restored shape clean.
            if m.get("is_compact_summary"):
                item["is_compact_summary"] = True
            restored.append(item)
        if not restored:
            return
        self._context.replace_all(restored)
        self._topic = str(snapshot.get("topic") or "")
        self._round = int(snapshot.get("round") or 0)
        session_path = str(snapshot.get("session_dir") or "").strip()
        if session_path:
            p = Path(session_path)
            if p.is_dir():
                self._state.session_dir = p
                self._state.save_current_session(
                    p,
                    topic=self._topic,
                    round_num=self._round,
                    agents=list(self._active_agents),
                    leader=self._leader,
                )
        logger.info(
            "Restored chat history: {} messages, topic={!r}, round={}",
            len(restored), self._topic, self._round,
        )

    def _add_message(self, sender: str, content: str) -> None:
        """Append a message — through the ConversationContext seam."""
        self._context.add(sender, content)
        self._persist_chat_state()

    def _save_event(
        self,
        event_type: str,
        *,
        agent: str = "",
        content: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Delegate to persistence layer + phase-0 observability log."""
        from nanobot.groupchat.room_observability import emit_room_event

        merged_extra = dict(extra or {})
        if self._session_dir:
            merged_extra.setdefault("session_id", self._session_dir.name)
        emit_room_event(
            room_id=self.room_id,
            kind=event_type,
            source="session",
            agent=agent,
            content=content,
            extra=merged_extra or None,
        )
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
        """Compress history if needed — through the ConversationContext seam."""
        # Microcompact pre-pass (no LLM): age old tool-log blocks before the
        # threshold check / AI summary, keeping history lean so maybe_compress
        # fires later and summarises a smaller middle region.
        self._context.microcompact()
        await self._context.maybe_compress()
        self._persist_chat_state()

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


# Re-exports for backward compatibility
from nanobot.groupchat.orchestra.chat_utils import (
    build_tool_log,
    log_request,
    reasoning_tokens_from_provider_meta,
)
from nanobot.groupchat.orchestra.direct_chat import direct_chat
from nanobot.groupchat.orchestra.tools.tool_chat import (
    chat_with_tools,
    resolve_max_tool_iterations,
)

__all__ = [
    "GroupChatEngine",
    "build_tool_log",
    "log_request",
    "reasoning_tokens_from_provider_meta",
    "direct_chat",
    "chat_with_tools",
    "resolve_max_tool_iterations",
]
