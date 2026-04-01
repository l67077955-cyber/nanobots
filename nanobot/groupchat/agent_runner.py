"""AgentRunner — manages a single agent's lifecycle in broadcast mode.

Encapsulates the tool_loop → auto-wait → reactivation cycle that was
previously a 200-line closure inside broadcast_round.

Each AgentRunner has:
- Explicit state tracking (PENDING → RUNNING → WAITING → DONE/FAILED)
- Display callbacks for tool activity
- Token/latency accounting
- Clean error handling with failure propagation via MailboxHub
"""

from __future__ import annotations

import asyncio
import json as _json
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.mailbox import MailboxHub, ConversationPool
from nanobot.groupchat.utils import build_tool_log, log_request


class AgentState(Enum):
    """Agent lifecycle states."""
    PENDING = auto()
    RUNNING = auto()
    WAITING = auto()
    DONE = auto()
    FAILED = auto()


@dataclass
class AgentResult:
    """Result from a completed AgentRunner."""
    name: str
    content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    state: AgentState = AgentState.DONE
    error: str | None = None
    latency: float = 0.0
    iterations: int = 0


class AgentRunner:
    """Manages a single agent's execution in broadcast mode.

    Lifecycle:
        1. run() starts the tool_loop
        2. After tool_loop finishes, enters auto-wait (mailbox.wait)
        3. If a message arrives, injects it and re-runs tool_loop
        4. Repeats until MAX_CYCLES reached or cancelled
    """

    def __init__(
        self,
        name: str,
        agent_idx: int,
        total_agents: int,
        *,
        engine: Any,
        mailbox: MailboxHub,
        pool: ConversationPool | None,
        tool_registry: Any,
        tool_defs: list[dict] | None,
        messages: list[dict[str, Any]],
        model: str,
        is_leader: bool = False,
        search_pool: Any = None,
    ):
        self.name = name
        self._idx = agent_idx
        self._total = total_agents
        self._engine = engine
        self._mailbox = mailbox
        self._pool = pool
        self._registry = tool_registry
        self._tool_defs = tool_defs
        self._messages = messages
        self._model = model
        self._is_leader = is_leader
        self._search_pool = search_pool

        # State
        self.state = AgentState.PENDING
        self.content: str = ""
        self.all_tools_used: list[str] = []
        self.total_iterations = 0
        self.total_latency = 0.0

        # Per-cycle tracking (for display callbacks)
        self._cycle_t0 = 0.0
        self._cycle_usage: dict[str, int] = {}

        # Display buffers
        self._tool_lines: list[str] = []
        self._pending_searches: list[str] = []

        # Cycle limits
        self.MAX_CYCLES = 6 if is_leader else 4
        self._max_iters = 12 if is_leader else 8

    async def run(self) -> AgentResult:
        """Main execution loop: tool_loop → auto-wait → repeat."""
        if self.name not in self._engine.registry:
            return AgentResult(name=self.name, state=AgentState.FAILED, error="Not in registry")

        model_short = self._model.split("/")[-1]
        self.state = AgentState.RUNNING

        # Send initial thinking status
        await self._engine._send(_d.thinking_msg(
            self.name, model_short,
            leader=self._engine._leader,
            idx=self._idx + 1, total=self._total,
        ))

        from nanobot.agent.tool_loop import tool_loop

        cycle = 0
        try:
            while cycle < self.MAX_CYCLES:
                cycle += 1
                self.state = AgentState.RUNNING
                self._cycle_t0 = _time.time()
                self._cycle_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

                async def _on_iter_usage(usage: dict) -> None:
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        self._cycle_usage[k] += usage.get(k, 0)

                result = await tool_loop(
                    provider=self._engine.provider,
                    messages=self._messages,
                    tool_registry=self._registry,
                    model=self._model,
                    max_tokens=self._engine.config.max_tokens,
                    max_iterations=self._max_iters,
                    tool_defs=self._tool_defs if self._tool_defs else None,
                    metadata={
                        "trace_name": f"broadcast_{self.name}_c{cycle}",
                        "trace_user_id": "groupchat",
                        "tags": [self.name, "broadcast"],
                        "generation_name": f"{self.name}_broadcast",
                        "debug_context": self._engine._debug_context,
                        "log_agent": self.name,
                        "log_mode": "broadcast",
                    },
                    on_tool_start=self._on_tool_start,
                    on_tool_result=self._on_tool_result,
                    on_iteration_usage=_on_iter_usage,
                    on_content_delta=None,
                    on_content_reset=None,
                    clean_response=lambda c: self._engine._clean_response(c, self.name),
                    result_max_chars=20_000,
                )

                # Flush any remaining buffered search lines
                await self._flush_searches()

                self.content = result.content or ""
                is_error = result.finish_reason == "error"
                self.total_latency += result.latency
                self.total_iterations += result.iterations
                self.all_tools_used.extend(result.tools_used or [])

                if is_error:
                    err_short = self.content[:150] if self.content else "Unknown error"
                    await self._engine._send(f"  ✗ {self.name} failed ({result.latency:.1f}s): {err_short}")
                    log_request(self._engine, self.name, self._model, "broadcast",
                                error=err_short, iterations=self.total_iterations,
                                latency=self.total_latency)
                    self.state = AgentState.FAILED
                    return AgentResult(
                        name=self.name, state=AgentState.FAILED,
                        error=err_short, latency=self.total_latency,
                    )

                # Record final text in history
                if self.content:
                    logger.info(
                        "broadcast [{}] cycle {} output ({}c): {}",
                        self.name, cycle, len(self.content), self.content,
                    )
                    history_content = self.content + build_tool_log(result.tool_calls_detail)
                    self._engine._add_message(self.name, history_content)
                    if self._search_pool:
                        self._search_pool.on_output(self.name)

                # Anti-idle guard
                _substantive_tools = {"web_search", "web_fetch", "exec", "read_file", "write_file"}
                if cycle == 1 and not self.content and not (set(result.tools_used or []) & _substantive_tools):
                    logger.warning(
                        "Broadcast: {} idle on cycle 1 (no content, tools={}), forcing retry",
                        self.name, result.tools_used,
                    )
                    self._messages.append({
                        "role": "system",
                        "content": (
                            f"[⚠️ 你（{self.name}）还没有采取任何行动！]\n"
                            "你必须立即使用工具（web_search, web_fetch, exec 等）来回答用户的最新问题。\n"
                            "不要直接从之前的对话中回答 — 用户需要新的搜索结果。\n"
                            "禁止调用 wait() — 先执行工作再交流。"
                        ),
                    })
                    continue

                # ── Auto-share if agent never used chatroom_send ──
                if self.content and "chatroom_send" not in self.all_tools_used:
                    snippet = self.content[:500]
                    self._mailbox.send(self.name, ["All"], snippet)
                    tok = result.token_usage
                    total_tok = tok.get("total", 0)
                    tok_suffix = ""
                    if total_tok > 0:
                        elapsed = _time.time() - self._cycle_t0
                        cost = result.cost or 0
                        cache_t = result.cache_tokens or 0
                        cost_str = f" ${cost:.4f}" if cost else ""
                        cache_str = f" 🔵{cache_t}" if cache_t else ""
                        tok_suffix = f"\n`in:{tok.get('prompt',0)} out:{tok.get('completion',0)} Σ{total_tok} · {elapsed:.1f}s{cost_str}{cache_str}`"
                    await self._engine._send(_d.chatroom_send_msg(
                        self.name, "All", self.content + tok_suffix,
                        max_len=3000, leader=self._engine._leader,
                    ))

                # ── Quick mailbox drain: check for pending messages ──
                # No blocking wait — just check if teammates sent us
                # something while we were working. If yes, inject and re-run.
                if cycle < self.MAX_CYCLES:
                    if self._pool:
                        self._pool.release_unread(self.name)

                    # Non-blocking: grab whatever is in the queue right now
                    msg = await self._mailbox.wait(self.name, timeout=3)

                    if msg is None:
                        # Nothing pending — this agent is done
                        logger.info("Broadcast: {} finished (cycle {}, no pending messages)", self.name, cycle)
                        break

                    # Got a message — inject and re-run
                    self.state = AgentState.RUNNING
                    logger.info("Broadcast: {} got pending msg from {}: {}", self.name, msg.sender, msg.content[:60])
                    await self._engine._send(_d.chatroom_wait_msg(self.name, str(msg), leader=self._engine._leader))

                    # Inject context for next cycle
                    if self.content:
                        self._messages.append({"role": "assistant", "content": self.content})
                    self._messages.append({
                        "role": "system",
                        "content": (
                            f"[提醒] 你（{self.name}）已经发表过上述观点。"
                            f"针对队友的新消息做出回应或补充新观点，不要重复已说的内容。"
                        ),
                    })
                    self._messages.append({
                        "role": "user",
                        "content": f"[队友消息] {msg}",
                    })

            # ── Final completion ──
            self.state = AgentState.DONE
            comp = _d.completion_msg(
                self.name, round(self.total_latency, 1),
                self.total_iterations, self.all_tools_used,
                leader=self._engine._leader,
            )
            if comp:
                await self._engine._send(comp)

            log_request(self._engine, self.name, self._model, "broadcast",
                        reply_len=len(self.content) if self.content else 0,
                        tools=self.all_tools_used, iterations=self.total_iterations,
                        latency=round(self.total_latency, 1))

            return AgentResult(
                name=self.name, content=self.content,
                tools_used=self.all_tools_used, state=AgentState.DONE,
                latency=self.total_latency, iterations=self.total_iterations,
            )

        except asyncio.CancelledError:
            self.state = AgentState.DONE
            comp = _d.completion_msg(
                self.name, round(self.total_latency, 1),
                self.total_iterations, self.all_tools_used,
                leader=self._engine._leader,
            )
            if comp:
                await self._engine._send(comp)
            return AgentResult(
                name=self.name, content=self.content or "",
                tools_used=self.all_tools_used, state=AgentState.DONE,
                latency=self.total_latency,
            )

        except Exception as e:
            self.state = AgentState.FAILED
            logger.error("Broadcast: {} failed: {}", self.name, e)
            await self._engine._send(f"  ✗ {self.name} error: {e}")
            log_request(self._engine, self.name, self._model, "broadcast", error=str(e))
            return AgentResult(
                name=self.name, state=AgentState.FAILED,
                error=str(e), latency=self.total_latency,
            )

        finally:
            if self._pool:
                self._pool.release_unread(self.name)
            if self.state == AgentState.FAILED:
                error_msg = f"LLM error" if not self.content else self.content[:100]
                self._mailbox.mark_agent_failed(self.name, error_msg)
            else:
                self._mailbox.mark_agent_done(self.name)

    # ── Display callbacks ──────────────────────────────────────

    async def _flush_searches(self) -> None:
        """Flush buffered search tool lines as one combined message."""
        if self._pending_searches:
            combined = "\n".join(self._pending_searches)
            await self._engine._send(combined)
            self._pending_searches.clear()

    async def _on_tool_start(self, tool_name: str, args: dict) -> None:
        if not isinstance(args, dict):
            args = {}
        # Persist tool_call event
        self._engine._save_event("tool_call", agent=self.name, extra={
            "tool": tool_name,
            "args": {k: (v if isinstance(v, str) else v) for k, v in args.items()},
        })
        logger.info(
            "broadcast [{}] tool_call: {}({})",
            self.name, tool_name, _json.dumps(args, ensure_ascii=False),
        )

        leader = self._engine._leader
        if tool_name == "chatroom_send":
            await self._flush_searches()
            to = args.get("to", "?")
            msg_full = (args.get("message", "") or "")
            to_str = ", ".join(to) if isinstance(to, list) else str(to)
            cost = len([a for a in [self.name] if False])  # placeholder
            if to_str.lower() == "all":
                cost = self._total - 1
            else:
                cost = len(to) if isinstance(to, list) else 1
            self._tool_lines.append(f"{self.name}: chatroom_send({to_str}) [cost={cost}]")
            # Build stats suffix
            elapsed = _time.time() - self._cycle_t0
            tok_t = self._cycle_usage.get("total_tokens", 0)
            stats_suffix = ""
            if tok_t > 0:
                p = self._cycle_usage["prompt_tokens"]
                c = self._cycle_usage["completion_tokens"]
                stats_suffix = f"\n`in:{p} out:{c} Σ{tok_t} · {elapsed:.1f}s`"
            await self._engine._send(_d.chatroom_send_msg(self.name, to_str, msg_full + stats_suffix, leader=leader))
        elif tool_name == "wait":
            await self._flush_searches()
            from_who = args.get("from_agent", "")
            self._tool_lines.append(f"{self.name}: wait({'来自 ' + from_who if from_who else '消息'})")
        elif tool_name in ("web_search", "web_fetch"):
            line = _d.tool_activity_msg(self.name, tool_name, args, leader=leader)
            self._tool_lines.append(line)
            self._pending_searches.append(line)
        else:
            await self._flush_searches()
            line = _d.tool_activity_msg(self.name, tool_name, args, leader=leader)
            self._tool_lines.append(line)
            await self._engine._send(line)

    async def _on_tool_result(self, tool_name: str, tool_call_id: str, result: str) -> None:
        self._engine._save_event("tool_result", agent=self.name, extra={
            "tool": tool_name,
            "result_len": len(result) if result else 0,
            "success": not (result or "").startswith("Error:"),
        })
        logger.info(
            "broadcast [{}] tool_result: {} ({}c): {}",
            self.name, tool_name, len(result) if result else 0, result,
        )

        leader = self._engine._leader
        if tool_name == "chatroom_send" and result:
            if "BLOCKED:" in result or "threads]" in result:
                if "BLOCKED:" in result:
                    pool_bar = _d.thread_bar(self._pool.used, self._pool.capacity) if self._pool else ""
                    await self._engine._send(f"✗ {self.name} dropped ── {pool_bar}")
                else:
                    if self._pool:
                        await self._engine._send(f"  {_d.thread_bar(self._pool.used, self._pool.capacity)}")
        elif tool_name == "wait" and result and not result.startswith("⏰"):
            await self._engine._send(_d.chatroom_wait_msg(self.name, result, leader=leader))
        elif tool_name in ("web_search", "web_fetch") and result:
            brief = _d.tool_result_brief(self.name, tool_name, result)
            if tool_name == "web_search" and self._search_pool:
                brief += f"  🔍 {self._search_pool.status()}"
            self._pending_searches.append(brief)
        elif tool_name == "exec" and result:
            await self._flush_searches()
            brief = _d.tool_result_brief(self.name, tool_name, result)
            await self._engine._send(brief)
