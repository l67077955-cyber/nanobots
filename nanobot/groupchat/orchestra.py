"""Orchestra execution mode for group chat.

Runs non-leader agents in parallel (Grok-style display),
then the leader synthesizes all findings into a final reply.

Visual style matches xAI's multi-agent output:
- Leader header: "👑 {Name}领导者"
- Agent sections: "Agent N (Name)" with tool activity
- Tool format: "已搜索的网络 query" / "已浏览 url"
- Consolidated per-agent editable messages
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.utils import log_request


@runtime_checkable
class OrchestraContext(Protocol):
    """Protocol documenting what orchestra_round needs from the engine.

    Mirrors ``BroadcastContext`` for consistency.
    """

    # ── Public attributes ──
    registry: dict[str, dict[str, Any]]
    tools: Any  # ToolRegistry
    direct_tools: Any  # ToolRegistry
    provider: Any  # LLMProvider
    config: Any  # GroupChatConfig

    # ── Private but accessed by orchestra ──
    _leader: str | None
    _debug_context: bool
    _history: list[dict[str, str]]
    _request_log: list[dict[str, Any]]
    _edit_fn: Callable[[int, str], Awaitable[None]] | None
    _send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None

    # ── Methods ──
    def _send(self, text: str) -> Awaitable[None]: ...
    def _save_event(self, event_type: str, *, agent: str = "", content: str = "", extra: dict | None = None) -> None: ...
    def _add_message(self, sender: str, content: str) -> None: ...
    def _clean_response(self, content: str, agent_name: str) -> str: ...
    def _build_agent_prompt(self, agent_name: str) -> list[dict[str, Any]]: ...
    def _get_agent_tools(self, agent_cfg: dict, registry: Any) -> list: ...
    def _agent_speak(self, agent_name: str, synthesis_context: str | None = None) -> Awaitable: ...


async def orchestra_round(
    speak_order: list[str],
    engine: OrchestraContext,
) -> None:
    """Run non-leader agents in parallel, then leader synthesizes.

    Args:
        speak_order: All agents in speaking order (leader last).
        engine: The GroupChatEngine instance.
    """
    leader = engine._leader
    others = [a for a in speak_order if a != leader]

    if not others or not leader:
        return

    total = len(speak_order)

    # ── Phase 0: Leader announcement ──
    await engine._send(
        f"👑 {leader}领导者\n"
        f"正在分析任务并协调 {len(others)} 个 Agent 并行工作..."
    )

    async def _run_agent_grok(
        name: str, agent_idx: int,
    ) -> tuple[str, tuple[str, list[str], dict] | None]:
        """Run one agent with Grok-style consolidated display."""
        if name not in engine.registry:
            return (name, None)

        agent_cfg = engine.registry[name]
        model = agent_cfg["model"]
        model_short = model.split("/")[-1]
        messages = engine._build_agent_prompt(name)

        # ── Consolidated editable message for this agent ──
        _lines: list[str] = []       # tool activity lines
        _msg_id: int | None = None
        _header = f"Agent {agent_idx + 1} ({name})"

        if engine._send_and_get_id_fn:
            _msg_id = await engine._send_and_get_id_fn(
                f"{_header}\n⏳ 思考中... ({model_short})"
            )

        async def _edit_consolidated() -> None:
            """Re-render the consolidated message."""
            if not (_msg_id and engine._edit_fn):
                return
            text = f"{_header}\n" + "\n\n".join(_lines)
            try:
                await engine._edit_fn(_msg_id, text[:4096])
            except Exception:
                pass

        # ── Grok-style tool callbacks ──

        async def _on_tool_start(tool_name: str, args: dict) -> None:
            if not isinstance(args, dict):
                args = {}
            if tool_name == "web_search":
                query = args.get("query", "")
                _lines.append(f"已搜索的网络\n{query}")
            elif tool_name == "web_fetch":
                url = args.get("url", "")
                short = url[:60] + ("..." if len(url) > 60 else "")
                _lines.append(f"已浏览\n{short}")
            elif tool_name == "exec":
                cmd = (args.get("command", "") or "")[:50]
                _lines.append(f"⚡ {cmd}")
            else:
                short = ""
                if args:
                    first = list(args.values())[0]
                    if isinstance(first, str):
                        short = first[:40]
                _lines.append(f"🔧 {tool_name}" + (f" {short}" if short else ""))
            await _edit_consolidated()

        async def _on_tool_result(
            tool_name: str, tool_call_id: str, result: str,
        ) -> None:
            if not result or not _lines:
                return
            rlen = len(result)
            last = _lines[-1]
            if tool_name == "web_search":
                import re as _re
                m = _re.search(r'\((\d+) results?\)', result[:100])
                count = int(m.group(1)) if m else max(result.count("\n") // 3, 1)
                _lines[-1] = f"{last}\n{count} 条结果"
            elif tool_name == "web_fetch":
                _lines[-1] = f"{last}\n({rlen}字)"
            else:
                preview = result.strip().replace("\n", " ")[:60]
                _lines[-1] = f"{last}\n↳ {preview}{'…' if rlen > 60 else ''}"
            await _edit_consolidated()

        # ── Streaming callback (accumulate text) ──
        _stream_buf: list[str] = []
        _last_edit: float = 0.0

        async def _on_delta(delta: str) -> None:
            nonlocal _last_edit
            _stream_buf.append(delta)
            now = _time.time()
            if (_msg_id and engine._edit_fn
                    and (now - _last_edit) >= 0.8):
                activity = "\n\n".join(_lines) + "\n\n" if _lines else ""
                text = f"{_header}\n{activity}" + "".join(_stream_buf) + " ▍"
                try:
                    await engine._edit_fn(_msg_id, text[:4096])
                except Exception:
                    pass
                _last_edit = now

        async def _on_reset() -> None:
            _stream_buf.clear()

        _delta_cb = _on_delta if (engine._edit_fn and engine._send_and_get_id_fn) else None
        _reset_cb = _on_reset if _delta_cb else None

        # ── Run LLM with tools ──
        try:
            content, tools_used, stats = await engine._chat_with_tools(
                messages=messages,
                model=model,
                agent_name=name,
                max_iterations=5,
                on_content_delta=_delta_cb,
                on_content_reset=_reset_cb,
                on_tool_start_override=_on_tool_start,
                on_tool_result_override=_on_tool_result,
            )
            is_error = stats.get("finish_reason") == "error"
            latency = stats.get("latency", 0)

            if is_error:
                err_short = content[:150] if content else "Unknown error"
                if _msg_id and engine._edit_fn:
                    try:
                        await engine._edit_fn(
                            _msg_id, f"{_header}\n⚠️ 失败 ({latency}s): {err_short}"
                        )
                    except Exception:
                        pass
                log_request(engine, name, model, "orchestra",
                            error=err_short, **stats)
                return (name, None)

            # ── Final consolidated display ──
            activity_text = "\n\n".join(_lines) if _lines else ""
            if content:
                engine._add_message(name, content)
                sep = "\n\n" if activity_text else ""
                final = f"{_header}\n{activity_text}{sep}{content}"
                if _msg_id and engine._edit_fn:
                    try:
                        await engine._edit_fn(_msg_id, final[:4096])
                    except Exception:
                        await engine._send(final[:4096])
                else:
                    await engine._send(final[:4096])
            elif _msg_id and engine._edit_fn:
                try:
                    await engine._edit_fn(
                        _msg_id, f"{_header}\n{activity_text}\n\n(空回复)" if activity_text
                        else f"{_header}\n(空回复)"
                    )
                except Exception:
                    pass

            log_request(engine, name, model, "orchestra",
                        reply_len=len(content), tools=tools_used, **stats)
            return (name, (content, tools_used, stats))

        except Exception as e:
            logger.error("Orchestra: {} failed: {}", name, e)
            if _msg_id and engine._edit_fn:
                try:
                    await engine._edit_fn(_msg_id, f"{_header}\n⚠️ 失败: {e}")
                except Exception:
                    pass
            log_request(engine, name, model, "orchestra",
                        error=str(e))
            return (name, None)

    # ── Run all non-leader agents concurrently ──
    tasks = [_run_agent_grok(name, si) for si, name in enumerate(others)]
    parallel_results = await asyncio.gather(*tasks, return_exceptions=True)

    # ── Phase 2: Build synthesis context ──
    research_parts: list[str] = []
    for item in parallel_results:
        if isinstance(item, Exception):
            logger.error("Orchestra parallel agent error: {}", item)
            continue
        name, result = item
        if result is None:
            research_parts.append(f"[{name}]: (请求失败，无结果)")
            continue
        content, tools_used, stats = result
        tool_str = f" | 工具: {', '.join(tools_used)}" if tools_used else ""
        tool_details = stats.get("tool_calls_detail", [])
        detail_lines = ""
        if tool_details:
            details = []
            for td in tool_details[:8]:
                t_name = td.get("name", "?")
                t_result = td.get("result_preview", "")[:3000]
                details.append(f"  - {t_name}: {t_result}")
            detail_lines = "\n" + "\n".join(details)
        research_parts.append(
            f"[{name}]{tool_str}:\n{content or '(空回复)'}{detail_lines}"
        )

    synthesis_context = (
        "[团队研究结果 — 请综合以下所有 agent 的发现，给出最终回复]\n"
        "[重要指令：\n"
        "1. 直接基于以下 agent 报告中的信息综合回答，不要再调用任何工具。\n"
        "2. 所有具体的版本号、日期、数值必须来自下面的 agent 报告。\n"
        "3. 优先引用日期最新的信息，如果多个 agent 报告了不同版本，以最新的为准。\n"
        "4. 信息不完整时如实说明即可。]\n\n"
        + "\n\n---\n\n".join(research_parts)
    )
    logger.info(
        "Orchestra synthesis context: {} agents, {} chars",
        len(research_parts), len(synthesis_context),
    )

    # ── Phase 3: Leader synthesizes ──
    await engine._send(
        f"\n👑 {leader}领导者\n"
        f"正在综合 {len(others)} 个 Agent 的研究结果..."
    )
    await engine._agent_speak(leader, synthesis_context=synthesis_context)
