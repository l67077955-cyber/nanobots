"""Per-round resource setup for broadcast mode."""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from nanobot.groupchat.orchestra.broadcast_context import BroadcastContext
from nanobot.groupchat.orchestra.broadcast_status import AgentStatusTracker
from nanobot.groupchat.orchestra.mailbox import MailboxHub, ConversationPool

_GC_SETTINGS_DEFAULTS = {
    "search_initial": 2,
    "search_earn_interval": 4,
    "allocate_timeout": 15,
    "call_timeout": 90,
    "leader_call_timeout": 120,
    "global_timeout": 600,
    "conv_keep_turns": 3,
}


_KNOWN_GC_KEYS = frozenset({
    "tool_initial", "tool_earn_per_output", "allocate_timeout",
    "context_pool_capacity", "context_points_per_agent",
    "call_timeout", "leader_call_timeout", "global_timeout",
    "conv_keep_turns", "memory_palace_path",
    "search_initial", "search_earn_interval", "tool_earn_interval",
})


def load_groupchat_settings() -> dict:
    """Load ~/.nanobot/groupchat_settings.json merged with defaults."""
    settings = dict(_GC_SETTINGS_DEFAULTS)
    path = Path.home() / ".nanobot" / "groupchat_settings.json"
    if path.exists():
        try:
            saved = _json.loads(path.read_text())
            if isinstance(saved, dict):
                for k, v in saved.items():
                    if k in _KNOWN_GC_KEYS:
                        settings[k] = v
                    else:
                        logger.warning(
                            "groupchat_settings: ignored unknown key '{}' "
                            "(sampling params belong in hyperparams.json)",
                            k,
                        )
        except Exception as e:
            logger.warning("groupchat_settings: load failed: {}", e)
    return settings


class BroadcastOrchestrator:
    """State manager for a single broadcast round."""
    
    def __init__(self, agents: list[str], engine: BroadcastContext, mailbox: MailboxHub):
        self.engine = engine
        self.mailbox = mailbox
        
        self.leader_name = engine._leader if hasattr(engine, '_leader') else None
        if self.leader_name and self.leader_name not in agents:
            self.leader_name = None
            
        self.exec_agents = list(agents)
        self.non_leader_agents = [a for a in agents if a != self.leader_name] if self.leader_name else list(agents)
        self.total = len(self.exec_agents)
        
        self.gc_settings = load_groupchat_settings()
                
        self.pool: Any = None
        self.tracker: AgentStatusTracker = None # type: ignore
        self.search_pool: Any = None
        self.leader_gate: Any = None
        self.agent_tool_registries: dict[str, Any] = {}
        self._search_cache: dict[str, tuple[str, str]] = {}
        
        self.leader_end_event = asyncio.Event()
        self._leader_agent_tasks: dict = {}
        self.tasks: dict[asyncio.Task, str] = {}
        self.all_tasks: set[asyncio.Task] = set()

    async def setup_tools_and_pools(self, spawn_fn: Callable[[str, int], asyncio.Task]) -> None:
        """Initialize all shared resources for the round."""
        from nanobot.tools.registry import ToolRegistry
        from nanobot.groupchat.orchestra.tools.chatroom_tools import (
            ChatroomSendTool, WaitTool, CachedSearchTool, SearchPool, LeaderGate,
            ManageAgentTool, EndDiscussionTool, TransferCreditsTool, ClearContextTool,
            QuoteMessageTool, ListMessagesTool,
        )
        from nanobot.tools.memory_palace import MemoryPalaceTool
        import os

        from nanobot.groupchat.display.visibility import per_agent_pool_capacities

        per_agent_cap = per_agent_pool_capacities(
            self.exec_agents, self.engine.registry, self.leader_name,
        )

        # ConversationPool: rank-based per-agent, with settings fallback
        pool_capacity_setting = self.gc_settings.get("context_pool_capacity", 0)
        if pool_capacity_setting > 0:
            # Settings override: uniform capacity
            self.pool = ConversationPool(capacity=pool_capacity_setting, agents=list(self.exec_agents))
        else:
            # Rank-based per-agent capacity
            self.pool = ConversationPool(agents=list(self.exec_agents), per_agent_capacity=per_agent_cap)
        self.pool.ALLOCATE_TIMEOUT = float(self.gc_settings["allocate_timeout"])
        
        await self.engine._send(f"🧵 对话池 {self.pool.status()}")

        self.tracker = AgentStatusTracker(
            agents=self.exec_agents,
            leader=self.leader_name,
            edit_fn=getattr(self.engine, '_edit_fn', None),
            send_and_get_id_fn=getattr(self.engine, '_send_and_get_id_fn', None),
        )
        await self.tracker.create_panel()

        points_per_agent = self.gc_settings.get("context_points_per_agent", 0)
        tool_initial = self.gc_settings.get("tool_initial", self.gc_settings.get("search_initial", 2))
        if points_per_agent > 0:
            # Settings override: uniform search credits
            search_initial = points_per_agent
        else:
            # Rank-based per-agent search credits (reuse per_agent_cap from pool)
            search_initial = per_agent_cap
        self.search_pool = SearchPool(
            agents=list(self.exec_agents),
            initial_per_agent=search_initial,
            earn_per_output=self.gc_settings.get("tool_earn_per_output", 0.25),
        )

        if self.leader_name:
            self.leader_gate = LeaderGate(self.leader_name)

        _palace_path = self.gc_settings.get("memory_palace_path", str(Path.home() / ".nanobot" / "mempalace" / "palace"))
        os.environ["MEMPALACE_PALACE_PATH"] = _palace_path
        memory_palace = MemoryPalaceTool()

        for name in self.exec_agents:
            base_reg = self.engine._get_agent_registry(name)
            registry = ToolRegistry()
            for tool_name in base_reg.tool_names:
                tool = base_reg.get(tool_name)
                if tool:
                    if tool_name == "web_search":
                        registry.register(CachedSearchTool(tool, name, self._search_cache, search_pool=self.search_pool))
                    elif tool_name not in ("chatroom_send", "wait"):
                        registry.register(tool)
            send_tool = ChatroomSendTool(
                mailbox=self.mailbox, agent_name=name, pool=self.pool,
                search_pool=self.search_pool, leader_gate=self.leader_gate,
            )
            wait_tool = WaitTool(mailbox=self.mailbox, agent_name=name, pool=self.pool)
            wait_tool._send_tool = send_tool
            registry.register(send_tool)
            registry.register(wait_tool)
            agent_cfg = self.engine.registry.get(name, {})
            session_cfg = None
            if hasattr(self.engine, "_session_tools_override") and name in self.engine._session_tools_override:
                session_cfg = self.engine._session_tools_override[name]
            from nanobot.groupchat.tool_policy import (
                forget_tool_enabled,
                memory_palace_tool_enabled,
            )
            if memory_palace_tool_enabled(agent_cfg, session_override=session_cfg):
                registry.register(memory_palace)
            if forget_tool_enabled(agent_cfg, session_override=session_cfg):
                from nanobot.tools.forget import ForgetTool
                registry.register(ForgetTool())
            registry.register(QuoteMessageTool(mailbox=self.mailbox))
            registry.register(ListMessagesTool(mailbox=self.mailbox))
            self.agent_tool_registries[name] = registry

        if self.leader_name and self.leader_name in self.agent_tool_registries:
            manage_tool = ManageAgentTool(
                exec_agents=self.non_leader_agents,
                agent_tasks=self._leader_agent_tasks,
                engine=self.engine,
                mailbox=self.mailbox,
                spawn_fn=spawn_fn,
            )
            end_tool = EndDiscussionTool(end_event=self.leader_end_event, engine=self.engine, mailbox=self.mailbox)
            transfer_tool = TransferCreditsTool(search_pool=self.search_pool, engine=self.engine)
            clear_ctx_tool = ClearContextTool(
                engine=self.engine,
                mailbox=self.mailbox,
                exec_agents=self.non_leader_agents,
                leader_name=self.leader_name,
            )
            self.agent_tool_registries[self.leader_name].register(manage_tool)
            self.agent_tool_registries[self.leader_name].register(end_tool)
            self.agent_tool_registries[self.leader_name].register(transfer_tool)
            self.agent_tool_registries[self.leader_name].register(clear_ctx_tool)
