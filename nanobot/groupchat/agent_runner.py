"""AgentRunner — 单个 agent 在广播模式下的生命周期管理。

架构：
    AgentRunner.run()
     ├─ tool_loop()      → 执行一轮 LLM + 工具调用
     ├─ mailbox.wait()   → 等待 leader/队友消息（30s 轮询）
     ├─ state_bus check  → 检查 leader 是否发了 stop 命令
     └─ 循环直到 MAX_CYCLES 或被 cancel

⚠️ 关键约束（agent 修改代码时注意）：
    1. tool_loop() 的参数签名必须完全匹配 nanobot.agent.tool_loop.tool_loop()
    2. state_bus 的方法名（set_agent_activity, update_agent 等）不可改
    3. mailbox.wait() 返回 None 表示超时，返回 Message 表示有消息
    4. _on_tool_start / _on_tool_result 是 tool_loop 的回调，签名不可改
"""

from __future__ import annotations

import asyncio
import json as _json
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.mailbox import MailboxHub
from nanobot.groupchat.streaming import StreamingDisplay
from nanobot.groupchat.utils import build_tool_log, log_request


# ── 数据类型 ──────────────────────────────────────────────────

class AgentState(Enum):
    PENDING = auto()
    RUNNING = auto()
    WAITING = auto()
    DONE    = auto()
    FAILED  = auto()


@dataclass
class AgentResult:
    name: str
    content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    state: AgentState = AgentState.DONE
    error: str | None = None
    latency: float = 0.0
    iterations: int = 0


# ── AgentRunner ───────────────────────────────────────────────

class AgentRunner:
    """单个 agent 的执行器。

    生命周期: PENDING → RUNNING → (tool_loop) → WAITING → (收到消息?) → RUNNING → ... → DONE
    退出条件: MAX_CYCLES 耗尽 / leader 发 stop 命令 / 被 cancel / 出错
    """

    def __init__(
        self,
        name: str,
        agent_idx: int,
        total_agents: int,
        *,
        engine: Any,           # GroupChatEngine 实例
        mailbox: MailboxHub,   # 消息总线
        pool: Any = None,      # 已弃用，保留参数兼容
        tool_registry: Any,    # ToolRegistry 实例
        tool_defs: list[dict] | None,
        messages: list[dict[str, Any]],  # 初始 prompt（leader 可定制）
        model: str,
        is_leader: bool = False,
        state_bus: Any = None,           # FileStateBus 用于状态同步
        idle_wait_timeout: int = 3,      # 保留参数兼容，实际用 30s 等 leader 命令
    ):
        self.name = name
        self._idx = agent_idx
        self._total = total_agents
        self._engine = engine
        self._mailbox = mailbox
        self._registry = tool_registry
        self._tool_defs = tool_defs
        self._messages = messages
        self._model = model
        self._is_leader = is_leader
        self._state_bus = state_bus
        self._idle_wait_timeout = idle_wait_timeout

        # 运行状态
        self.state = AgentState.PENDING
        self.content: str = ""
        self.all_tools_used: list[str] = []
        self.total_iterations = 0
        self.total_latency = 0.0

        # 每轮 token 计数（用于显示）
        self._cycle_t0 = 0.0
        self._cycle_usage: dict[str, int] = {}

        # 搜索结果缓冲（合并显示避免刷屏）
        self._pending_searches: list[str] = []

        # 循环上限：leader 多给几轮
        self.MAX_CYCLES = 6 if is_leader else 4
        self._max_iters = 12 if is_leader else 8

    # ── 主循环 ────────────────────────────────────────────────

    async def run(self) -> AgentResult:
        """主执行循环：tool_loop → 等待消息 → 注入 → 重新执行。"""
        if self.name not in self._engine.registry:
            return AgentResult(name=self.name, state=AgentState.FAILED, error="Not in registry")

        self.state = AgentState.RUNNING
        if self._state_bus:
            self._state_bus.set_agent_activity(self.name, "thinking")

        # 显示 "正在思考..."
        model_short = self._model.split("/")[-1]
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
                if self._state_bus:
                    self._state_bus.set_agent_activity(self.name, "thinking", cycle=cycle)
                self._cycle_t0 = _time.time()
                self._cycle_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

                # token 计数回调
                async def _on_usage(usage: dict) -> None:
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        self._cycle_usage[k] += usage.get(k, 0)

                # Leader 流式显示：让用户实时看到 leader 的推理/分析
                if self._is_leader and self._engine._send_and_get_id_fn and self._engine._edit_fn:
                    _stream = StreamingDisplay(
                        f"👑 {self.name} ━━━━━━━━\n\n",
                        self._engine._send_and_get_id_fn,
                        self._engine._edit_fn,
                    )
                    _on_delta = _stream.on_delta
                    _on_reset = _stream.on_reset
                else:
                    _stream = None
                    _on_delta = None
                    _on_reset = None

                # ⚠️ 不可修改 — 参数必须完全匹配 tool_loop() 签名
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
                    on_iteration_usage=_on_usage,
                    on_content_delta=_on_delta,
                    on_content_reset=_on_reset,
                    clean_response=lambda c: self._engine._clean_response(c, self.name),
                    result_max_chars=20_000,
                )

                # 刷新搜索缓冲
                await self._flush_searches()

                # 处理结果
                self.content = result.content or ""
                self.total_latency += result.latency
                self.total_iterations += result.iterations
                self.all_tools_used.extend(result.tools_used or [])

                # 同步到 state.yaml
                if self._state_bus:
                    self._state_bus.update_agent(
                        self.name,
                        content_preview=(self.content[:200] if self.content else ""),
                        latency=round(self.total_latency, 2),
                        iterations=self.total_iterations,
                    )

                # Leader 流式完成 — finalize streaming message
                if _stream and self.content:
                    await _stream.finalize(
                        self.content,
                        fallback_send=self._engine._send,
                        max_len=4096,
                    )
                elif _stream:
                    # 流式消息存在但无文本 content（纯工具调用轮）→ 清理
                    if _stream.msg_id and self._engine._edit_fn:
                        try:
                            await self._engine._edit_fn(_stream.msg_id, f"👑 {self.name} ━━ (工具执行中)")
                        except Exception:
                            pass

                # 错误 → 立即退出
                if result.finish_reason == "error":
                    err = self.content[:150] if self.content else "Unknown error"
                    await self._engine._send(f"  ✗ {self.name} failed: {err}", sender=self.name)
                    self.state = AgentState.FAILED
                    if self._state_bus:
                        self._state_bus.set_agent_activity(self.name, "idle")
                    return AgentResult(name=self.name, state=AgentState.FAILED, error=err, latency=self.total_latency)

                # 记录到对话历史
                if self.content:
                    self._engine._add_message(self.name, self.content + build_tool_log(result.tool_calls_detail))

                # 防空转：第一轮没产出 → 强制重试
                _work_tools = {"web_search", "web_fetch", "exec", "read_file", "write_file"}
                if cycle == 1 and not self.content and not (set(result.tools_used or []) & _work_tools):
                    self._messages.append({
                        "role": "system",
                        "content": f"[⚠️ 你（{self.name}）还没有采取任何行动！] 请立即使用工具开始工作。",
                    })
                    continue

                # ⚠️ 不可修改 — Leader 控制的生命周期循环
                # Agent 不自行决定退出，等待 leader 的 stop/end_round 命令。
                if cycle < self.MAX_CYCLES:
                    if self._state_bus:
                        self._state_bus.set_agent_activity(self.name, "idle")

                    # 等待消息（30s 轮询）
                    msg = await self._mailbox.wait(self.name, timeout=30)

                    if msg is not None:
                        # 收到消息 → 注入上下文，重新执行
                        self.state = AgentState.RUNNING
                        if self._state_bus:
                            self._state_bus.set_agent_activity(self.name, "thinking")
                        await self._engine._send(
                            _d.chatroom_wait_msg(self.name, str(msg), leader=self._engine._leader),
                            sender=self.name,
                        )
                        if self.content:
                            self._messages.append({"role": "assistant", "content": self.content})
                        self._messages.append({"role": "system", "content": f"[提醒] 你（{self.name}）已发表过观点。针对新消息回应，不要重复。"})
                        self._messages.append({"role": "user", "content": f"[队友消息] {msg}"})
                        continue

                    # 没消息 → 检查 leader 是否修改了控制变量
                    if self._state_bus:
                        try:
                            data = self._state_bus._read_all()
                            # Agent block 被 leader 删掉 → 退出
                            if self.name not in data.get("agents", {}):
                                break
                            # state 被改为 paused → 退出
                            st = data["agents"][self.name].get("state", "")
                            if st == "paused":
                                break
                            # session.status 被改为 done → leader 结束群聊
                            session_status = data.get("session", {}).get("status", "")
                            if session_status == "done":
                                break
                        except Exception:
                            pass

                    # All agents idle — no one will produce new messages.
                    # Break out to avoid tight-looping.
                    if self._mailbox._all_waiting.is_set():
                        logger.info("AgentRunner {}: all agents idle, exiting", self.name)
                        break

                    continue  # 继续等待 — leader 控制生命周期

            # ── 正常完成 ──
            self.state = AgentState.DONE
            if self._state_bus:
                self._state_bus.set_agent_activity(self.name, "idle")

        except asyncio.CancelledError:
            self.state = AgentState.DONE  # cancel 也视为正常完成
        except Exception as e:
            self.state = AgentState.FAILED
            logger.error("AgentRunner {}: {}", self.name, e)
            await self._engine._send(f"  ✗ {self.name} error: {e}")

        # 显示完成消息
        comp = _d.completion_msg(self.name, round(self.total_latency, 1),
                                 self.total_iterations, self.all_tools_used,
                                 leader=self._engine._leader)
        if comp:
            await self._engine._send(comp)

        # 标记 mailbox 状态
        if self.state == AgentState.FAILED:
            self._mailbox.mark_agent_failed(self.name, self.content[:100] if self.content else "error")
        else:
            self._mailbox.mark_agent_done(self.name)

        return AgentResult(
            name=self.name, content=self.content,
            tools_used=self.all_tools_used, state=self.state,
            latency=self.total_latency, iterations=self.total_iterations,
            error=self.content[:150] if self.state == AgentState.FAILED else None,
        )

    # ── 显示回调 ──────────────────────────────────────────────

    async def _flush_searches(self) -> None:
        """合并显示缓冲的搜索结果。"""
        if self._pending_searches:
            await self._engine._send("\n".join(self._pending_searches))
            self._pending_searches.clear()

    async def _on_tool_start(self, tool_name: str, args: dict) -> None:
        """⚠️ 签名不可改 — tool_loop 回调。"""
        if not isinstance(args, dict):
            args = {}
        self._last_tool_args = args

        # 记录到 state.yaml
        if self._state_bus:
            self._state_bus.append_tool_start(self.name, tool_name, args)

        leader = self._engine._leader

        if tool_name == "chatroom_send":
            await self._flush_searches()
            to = args.get("to", "?")
            msg = args.get("message", "")
            to_str = ", ".join(to) if isinstance(to, list) else str(to)
            # 附加 token 统计
            tok = self._cycle_usage.get("total_tokens", 0)
            stats = ""
            if tok > 0:
                p, c = self._cycle_usage["prompt_tokens"], self._cycle_usage["completion_tokens"]
                stats = f"\n`in:{p} out:{c} Σ{tok} · {_time.time() - self._cycle_t0:.1f}s`"
            await self._engine._send(
                _d.chatroom_send_msg(self.name, to_str, msg + stats, leader=leader),
                sender=self.name,
            )
        elif tool_name == "wait":
            await self._flush_searches()
        elif tool_name in ("web_search", "web_fetch"):
            self._pending_searches.append(_d.tool_activity_msg(self.name, tool_name, args, leader=leader))
        else:
            await self._flush_searches()
            await self._engine._send(
                _d.tool_activity_msg(self.name, tool_name, args, leader=leader),
                sender=self.name,
            )

    async def _on_tool_result(self, tool_name: str, tool_call_id: str, result: str) -> None:
        """⚠️ 签名不可改 — tool_loop 回调。"""
        if self._state_bus:
            ok = not (result or "").startswith("Error:")
            self._state_bus.complete_tool(self.name, tool_name, len(result) if result else 0, ok, (result[:200] if result else ""))

        # 显示特定工具结果
        if tool_name == "wait" and result and not result.startswith("⏰"):
            await self._engine._send(_d.chatroom_wait_msg(self.name, result, leader=self._engine._leader))
        elif tool_name in ("web_search", "web_fetch") and result:
            self._pending_searches.append(_d.tool_result_brief(self.name, tool_name, result))
        elif tool_name == "exec" and result:
            await self._flush_searches()
            await self._engine._send(_d.tool_result_brief(self.name, tool_name, result))
