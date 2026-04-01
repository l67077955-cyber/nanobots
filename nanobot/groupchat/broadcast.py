"""Broadcast execution mode for group chat.

Orchestrates concurrent agent execution using:
- ``BroadcastCoordinator``: Setup, run, and synthesize phases
- ``AgentRunner`` (from agent_runner.py): Per-agent lifecycle management
- ``MailboxHub`` (from mailbox.py): Inter-agent messaging with failure awareness

Architecture:
    broadcast_round() → BroadcastCoordinator
        .setup()      — tools, prompts, pools
        .run()        — concurrent tasks + sentinels + user listener
        .synthesize() — leader summary or auto-summary
"""

from __future__ import annotations

import asyncio
import copy
import json as _json
import time as _time
from pathlib import Path
from typing import Any, Awaitable, Protocol, runtime_checkable

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.agent_runner import AgentRunner, AgentResult, AgentState
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool
from nanobot.groupchat.utils import log_request


# ── Protocol: documents what broadcast needs from the engine ──

@runtime_checkable
class BroadcastContext(Protocol):
    """Protocol documenting what broadcast_round needs from the engine."""

    registry: dict[str, dict[str, Any]]
    tools: Any
    provider: Any
    config: Any

    _round: int
    _leader: str | None
    _debug_context: bool
    _history: list[dict[str, str]]
    _request_log: list[dict[str, Any]]
    _session_dir: Any

    def _send(self, text: str) -> Awaitable[None]: ...
    def _save_event(self, event_type: str, *, agent: str = "", content: str = "", extra: dict | None = None) -> None: ...
    def _add_message(self, sender: str, content: str) -> None: ...
    def _save_round_summary(self, round_num: int, agents_responded: int, comm_count: int = 0, duration: float = 0.0) -> None: ...
    def _clean_response(self, content: str, agent_name: str) -> str: ...
    def _build_agent_prompt(self, agent_name: str) -> list[dict[str, Any]]: ...
    def _get_agent_tools(self, agent_cfg: dict, registry: Any) -> list: ...
    def _agent_speak(self, agent_name: str, no_tools: bool = False, no_stream: bool = False, silent: bool = False) -> Awaitable: ...

    @property
    def prompt_builder(self) -> Any: ...


# ── Helper: load groupchat settings ──

def _load_gc_settings() -> dict:
    """Load groupchat settings from disk with defaults."""
    defaults = {"search_initial": 2, "search_earn_interval": 4, "allocate_timeout": 15}
    path = Path.home() / ".nanobot" / "groupchat_settings.json"
    settings = dict(defaults)
    if path.exists():
        try:
            settings.update(_json.loads(path.read_text()))
        except Exception:
            pass
    return settings


def _extract_user_question(history: list[dict[str, str]]) -> str:
    """Get the most recent user message."""
    for msg in reversed(history):
        if msg.get("sender") in ("User", "user", "用户", "系统"):
            return msg.get("content", "")[:300]
    return ""


# ══════════════════════════════════════════════════════════════════
# BroadcastCoordinator — the single orchestrator
# ══════════════════════════════════════════════════════════════════

class BroadcastCoordinator:
    """Orchestrates a broadcast round: setup → run → synthesize.

    Replaces the previous 880-line broadcast_round function with
    a structured class that separates concerns into phases.
    """

    def __init__(
        self,
        agents: list[str],
        engine: BroadcastContext,
        mailbox: MailboxHub,
        global_timeout: float = 200.0,
    ):
        self.agents = list(agents)
        self.engine = engine
        self.mailbox = mailbox
        self.global_timeout = global_timeout

        # Detect leader
        self.leader_name = engine._leader if hasattr(engine, '_leader') else None
        if self.leader_name and self.leader_name not in agents:
            self.leader_name = None

        self.exec_agents = list(agents)
        self.non_leader_agents = [a for a in agents if a != self.leader_name] if self.leader_name else list(agents)
        self.total = len(self.exec_agents)

        # State
        self.pool: ConversationPool | None = None
        self.search_pool: Any = None
        self.leader_gate: Any = None
        self.leader_end_event = asyncio.Event()
        self.runners: dict[str, AgentRunner] = {}
        self.results: list[AgentResult] = []
        self._original_settings: dict[str, dict] = {}
        self._agent_tool_registries: dict[str, Any] = {}
        self._round_t0 = 0.0
        self._user_question = ""
        self.gc_settings: dict = {}

    # ── Phase 1: Setup ──────────────────────────────────────────

    def setup(self) -> None:
        """Initialize pools, tools, prompts, and AgentRunners."""
        self._round_t0 = _time.time()
        self.gc_settings = _load_gc_settings()
        self._user_question = _extract_user_question(self.engine._history)

        # Snapshot settings for restoration
        if self.leader_name:
            for name in self.agents:
                cfg = self.engine.registry.get(name, {})
                self._original_settings[name] = {
                    "tools": copy.deepcopy(cfg.get("tools", {})),
                }

        self._setup_pools()
        self._setup_tools()
        self._setup_runners()

    def _setup_pools(self) -> None:
        """Create ConversationPool and SearchPool."""
        from nanobot.groupchat.search_tools import SearchPool

        n = len(self.exec_agents)
        pool_cap_setting = self.gc_settings.get("context_pool_capacity", 0)
        pool_capacity = pool_cap_setting if pool_cap_setting > 0 else max(n * (n - 1), 2)
        self.pool = ConversationPool(capacity=pool_capacity, agents=list(self.exec_agents))

        points_per_agent = self.gc_settings.get("context_points_per_agent", 0)
        search_initial = points_per_agent if points_per_agent > 0 else self.gc_settings["search_initial"]
        self.search_pool = SearchPool(
            agents=list(self.exec_agents),
            initial_per_agent=search_initial,
            earn_interval=self.gc_settings["search_earn_interval"],
        )

    def _setup_tools(self) -> None:
        """Build per-agent tool registries with chatroom tools."""
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.groupchat.chatroom_tools import ChatroomSendTool
        from nanobot.groupchat.search_tools import CachedSearchTool
        from nanobot.groupchat.leader_tools import (
            LeaderGate, ManageAgentTool, EndDiscussionTool, TransferCreditsTool,
        )

        _search_cache: dict[str, tuple[str, str]] = {}

        # Leader gate
        if self.leader_name:
            self.leader_gate = LeaderGate(self.leader_name)

        for name in self.exec_agents:
            base_reg = self.engine._get_agent_registry(name)
            registry = ToolRegistry()

            # Copy existing tools, wrapping web_search with cache
            for tool_name in base_reg.tool_names:
                tool = base_reg.get(tool_name)
                if tool:
                    if tool_name == "web_search":
                        registry.register(CachedSearchTool(tool, name, _search_cache, search_pool=self.search_pool))
                    elif tool_name not in ("chatroom_send", "wait"):
                        registry.register(tool)

            # Add chatroom_send only (no wait tool — agents finish naturally)
            send_tool = ChatroomSendTool(
                mailbox=self.mailbox, agent_name=name, pool=self.pool,
                search_pool=self.search_pool, leader_gate=self.leader_gate,
            )
            registry.register(send_tool)
            self._agent_tool_registries[name] = registry

        # Leader-specific tools
        self._leader_agent_tasks: dict = {}
        if self.leader_name and self.leader_name in self._agent_tool_registries:
            manage_tool = ManageAgentTool(
                exec_agents=self.non_leader_agents,
                agent_tasks=self._leader_agent_tasks,
                engine=self.engine,
                mailbox=self.mailbox,
            )
            end_tool = EndDiscussionTool(end_event=self.leader_end_event, engine=self.engine)
            transfer_tool = TransferCreditsTool(search_pool=self.search_pool, engine=self.engine)
            self._agent_tool_registries[self.leader_name].register(manage_tool)
            self._agent_tool_registries[self.leader_name].register(end_tool)
            self._agent_tool_registries[self.leader_name].register(transfer_tool)

    def _setup_runners(self) -> None:
        """Create AgentRunner instances with built prompts."""
        for idx, name in enumerate(self.exec_agents):
            if name not in self.engine.registry:
                continue

            agent_cfg = self.engine.registry[name]
            model = agent_cfg["model"]
            is_leader = (name == self.leader_name)

            # Build prompt using prompt_builder
            messages = self.engine.prompt_builder.build_broadcast_prompt(
                name,
                engine=self.engine,
                agents=self.exec_agents,
                user_question=self._user_question,
                leader_name=self.leader_name,
                agent_idx=idx,
                total=self.total,
                search_pool=self.search_pool,
            )

            # Determine tool definitions
            reg = self._agent_tool_registries[name]
            tool_defs = self.engine._get_agent_tools(agent_cfg, reg)
            broadcast_tool_names = ["chatroom_send"]
            if is_leader:
                broadcast_tool_names.extend(["manage_agent", "end_discussion", "transfer_credits"])
            broadcast_defs = [
                t.to_schema() for t in [reg.get(tn) for tn in broadcast_tool_names]
                if t is not None
            ]
            if tool_defs:
                existing_names = {d["function"]["name"] for d in tool_defs}
                for bd in broadcast_defs:
                    if bd["function"]["name"] not in existing_names:
                        tool_defs.append(bd)
            else:
                tool_defs = broadcast_defs

            runner = AgentRunner(
                name, idx, self.total,
                engine=self.engine,
                mailbox=self.mailbox,
                pool=self.pool,
                tool_registry=reg,
                tool_defs=tool_defs,
                messages=messages,
                model=model,
                is_leader=is_leader,
                search_pool=self.search_pool,
            )
            self.runners[name] = runner

    # ── Phase 2: Run ────────────────────────────────────────────

    async def run(self) -> None:
        """Execute all agents concurrently with sentinels and user listener."""
        # Announce
        self.engine._save_event("round_start", extra={
            "round": self.engine._round + 1,
            "agents": self.agents,
            "mode": "broadcast",
            "leader": self.leader_name,
        })
        await self.engine._send(_d.broadcast_start_msg(
            self.agents, int(self.global_timeout), leader=self.leader_name,
        ))
        if self.pool:
            await self.engine._send(f"── threads {_d.thread_bar(0, self.pool.capacity)} ──")

        # Setup mailboxes
        for name in self.exec_agents:
            self.mailbox.create(name)
        self.mailbox.start_round(active_agents=list(self.exec_agents))

        # Launch agent tasks
        tasks: dict[asyncio.Task, str] = {}
        for name, runner in self.runners.items():
            task = asyncio.create_task(runner.run())
            tasks[task] = name

        # Populate leader's task mapping for ManageAgentTool
        for task_obj, task_name in tasks.items():
            if task_name != self.leader_name:
                self._leader_agent_tasks[task_obj] = task_name

        # Launch sentinels and listener
        _user_listener_running = True

        async def _user_listener() -> None:
            while _user_listener_running:
                try:
                    msg = await asyncio.wait_for(self.engine._input_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg == "__SUMMARY__":
                    continue

                if self.pool:
                    await self.pool.allocate_user(list(self.agents))
                self.mailbox.create("用户")
                self.mailbox.send("用户", ["All"], msg)
                self.engine._add_message("用户", msg)
                pool_bar = _d.thread_bar(self.pool.used, self.pool.capacity) if self.pool else ""
                await self.engine._send(f"── User ──\n{msg}\n  {pool_bar}")
                logger.info("Broadcast: user interjected: {}", msg[:60])

        async def _watch_leader_end() -> None:
            await self.leader_end_event.wait()

        user_task = asyncio.create_task(_user_listener())
        leader_sentinel = asyncio.create_task(_watch_leader_end())
        # Only monitor agent tasks + leader sentinel (no idle sentinel, no timeout)
        all_monitored = set(tasks.keys()) | {leader_sentinel}

        completed = 0
        SAFETY_LIMIT = 600  # absolute ceiling to prevent infinite loops
        try:
            while not all(t.done() for t in tasks.keys()):
                done_set, _ = await asyncio.wait(
                    [t for t in all_monitored if not t.done()],
                    timeout=SAFETY_LIMIT,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done_set:
                    # Safety limit reached
                    logger.warning("Broadcast: safety limit {}s reached", SAFETY_LIMIT)
                    for task_obj in tasks:
                        if not task_obj.done():
                            task_obj.cancel()
                    break

                should_break = False
                for t in done_set:
                    if t is leader_sentinel:
                        logger.info("Broadcast: leader ended discussion")
                        await self.engine._send("━━ Leader 结束讨论 — entering synthesis ━━")
                        for task_obj in tasks:
                            if not task_obj.done():
                                task_obj.cancel()
                        should_break = True
                        break
                    elif t in tasks:
                        try:
                            result = t.result()
                            completed += 1
                            self.results.append(result)
                            logger.info(
                                "Broadcast: {}/{} done — {} ({})",
                                completed, self.total, result.name,
                                f"{len(result.content)} chars" if result.content else "empty",
                            )
                        except Exception as e:
                            completed += 1
                            logger.error("Broadcast: agent task error: {}", e)
                            await self.engine._send(f"\u2717 Agent error: {e}")

                if should_break:
                    break

        except Exception as e:
            logger.error("Broadcast: run loop error: {}", e)

        # Cleanup
        if not leader_sentinel.done():
            leader_sentinel.cancel()
        _user_listener_running = False
        if not user_task.done():
            user_task.cancel()
        for task_obj in tasks:
            if not task_obj.done():
                task_obj.cancel()
                logger.warning("Broadcast: {} cancelled", tasks[task_obj])

        # Collect any remaining results
        for task_obj, name in tasks.items():
            if task_obj.done() and not any(r.name == name for r in self.results):
                try:
                    result = task_obj.result()
                    self.results.append(result)
                except Exception:
                    pass

        # Round summary
        comm_count = len(self.mailbox.history)
        round_duration = _time.time() - self._round_t0
        self.engine._save_round_summary(
            round_num=self.engine._round + 1,
            agents_responded=len(self.results),
            comm_count=comm_count,
            duration=round_duration,
        )
        await self.engine._send(_d.broadcast_complete_msg(len(self.results), self.total, comm_count))

        # Chat chain summary
        chain = _d.chat_chain_summary(self.mailbox.history, leader=self.leader_name)
        if chain:
            await self.engine._send(chain)

        self.mailbox.clear()

    # ── Phase 3: Synthesize ─────────────────────────────────────

    async def synthesize(self) -> None:
        """Post-round synthesis: leader summary or auto-summary."""
        if self.leader_name and self.leader_name in self.agents:
            await self._leader_synthesis()
        else:
            from nanobot.groupchat.run_loop import generate_summary
            await generate_summary(self.engine)

        # Restore original settings
        if self._original_settings:
            for name, orig in self._original_settings.items():
                cfg = self.engine.registry.get(name)
                if cfg and orig.get("tools"):
                    cfg["tools"] = orig["tools"]
            logger.info("Broadcast: restored original agent settings")

    async def _leader_synthesis(self) -> None:
        """Leader evaluates all agent outputs and produces final summary."""
        leader_model = self.engine.registry[self.leader_name]["model"]
        model_short = leader_model.split("/")[-1]
        await self.engine._send(f"━━ {self.leader_name} ({model_short}) · 总结 ━━")

        agent_outputs = []
        leader_own_output = ""
        for result in self.results:
            if result.content:
                if result.name == self.leader_name:
                    leader_own_output = result.content
                else:
                    agent_outputs.append(f"[{result.name} 的回复]\n{result.content}")

        chat_msgs = []
        for msg in self.mailbox.history:
            sender = msg.sender if hasattr(msg, "sender") else str(msg.get("sender", "?"))
            content_text = msg.content if hasattr(msg, "content") else str(msg.get("content", ""))
            chat_msgs.append(f"[{sender}]: {content_text[:500]}")

        synthesis_context = f"[Leader 最终总结]\n原始问题: {self._user_question}\n\n"
        if agent_outputs:
            synthesis_context += f"各 agent 结果:\n" + "\n\n".join(agent_outputs) + "\n\n"
        if chat_msgs:
            synthesis_context += f"对话记录:\n" + "\n".join(chat_msgs) + "\n\n"
        if leader_own_output:
            synthesis_context += f"你之前的发言:\n{leader_own_output}\n\n"
        synthesis_context += (
            "请基于以上所有信息，给出完整、结构化的最终总结。\n"
            "整合所有发现，评价各 agent 的表现，指出亮点和不足，给出结论。"
        )

        try:
            await self.engine._agent_speak(
                self.leader_name,
                synthesis_context=synthesis_context,
            )
        except Exception as e:
            logger.error("Leader synthesis failed: {}", e)
            await self.engine._send(f"✗ {self.leader_name} 总结失败: {e}")

    # ── Results ─────────────────────────────────────────────────

    def get_results(self) -> list[tuple[str, str | None]]:
        """Return results in the old (name, content) format."""
        return [(r.name, r.content) for r in self.results]


# ══════════════════════════════════════════════════════════════════
# Public API — drop-in replacement for the old broadcast_round
# ══════════════════════════════════════════════════════════════════

async def broadcast_round(
    agents: list[str],
    engine: BroadcastContext,
    mailbox: MailboxHub,
    global_timeout: float = 200.0,
) -> list[tuple[str, str | None]]:
    """Run all agents concurrently with out-of-order completion display.

    Drop-in replacement for the old 880-line function. Now delegates
    to BroadcastCoordinator for clean phase separation.

    Args:
        agents: List of agent names to run.
        engine: The GroupChatEngine instance.
        mailbox: Shared MailboxHub for inter-agent communication.
        global_timeout: Hard limit for the entire round (seconds).

    Returns:
        List of (agent_name, content) tuples in completion order.
    """
    if not agents:
        return []

    coordinator = BroadcastCoordinator(agents, engine, mailbox, global_timeout)

    # Phase 1: Setup
    coordinator.setup()

    # Phase 2: Run agents concurrently
    await coordinator.run()

    # Phase 3: Synthesis
    await coordinator.synthesize()

    return coordinator.get_results()
