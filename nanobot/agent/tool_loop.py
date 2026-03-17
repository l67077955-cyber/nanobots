"""Shared LLM + tool-calling loop.

Provides a single ``tool_loop()`` coroutine that both ``AgentLoop`` (core)
and ``GroupChatEngine`` (group chat) delegate to for the iterative
call-LLM → execute-tools → append-messages cycle.

Callers customise behaviour through optional callbacks rather than
subclassing, keeping the core loop logic in one place.
"""

from __future__ import annotations

import json
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.utils.helpers import build_assistant_message


# ── Result ────────────────────────────────────────────────────────────────

@dataclass
class ToolLoopResult:
    """Result returned by :func:`tool_loop`."""

    content: str | None
    """Final text response (cleaned if *clean_response* was provided)."""

    tools_used: list[str] = field(default_factory=list)
    """Ordered list of tool names that were invoked."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    """The full message list after the loop completes (including new
    assistant/tool messages appended during execution)."""

    iterations: int = 0
    """Number of LLM round-trips executed."""

    latency: float = 0.0
    """Cumulative wall-clock seconds across all LLM calls."""

    token_usage: dict[str, int] = field(default_factory=lambda: {
        "prompt": 0, "completion": 0, "total": 0,
    })
    """Accumulated token counters."""

    call_details: list[dict[str, Any]] = field(default_factory=list)
    """Per-iteration records (latency, tokens, finish_reason, tools)."""

    tool_calls_detail: list[dict[str, Any]] = field(default_factory=list)
    """Per-tool-call records (name, args preview, result length/preview)."""

    tools_available: bool = False
    """Whether tool definitions were provided for this call."""

    finish_reason: str = "stop"
    """``"stop"`` for normal completion, ``"max_iterations"``, or ``"error"``."""

    status_code: int | None = None
    """HTTP status code on error (e.g. 429, 502)."""


# ── Helpers ───────────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>")


def _strip_think(text: str | None) -> str | None:
    """Remove ``<think>…</think>`` blocks embedded by some models."""
    if not text:
        return None
    return _THINK_RE.sub("", text).strip() or None


# ── Main loop ─────────────────────────────────────────────────────────────

async def tool_loop(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    tool_registry: ToolRegistry,
    *,
    model: str,
    max_tokens: int = 4096,
    max_iterations: int = 5,
    tool_defs: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    # ── Callbacks ──
    on_tool_start: Callable[[str, dict], Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, str, str], Awaitable[None]] | None = None,
    on_thought: Callable[[str], Awaitable[None]] | None = None,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_content_reset: Callable[[], Awaitable[None]] | None = None,
    clean_response: Callable[[str], str] | None = None,
    build_message: Callable[..., dict[str, Any]] | None = None,
    result_max_chars: int = 8000,
) -> ToolLoopResult:
    """Run the LLM → tool → repeat loop.

    Parameters
    ----------
    on_content_delta:
        ``async (delta_text) -> None`` — called with each text chunk during
        streaming of the final response.  Requires ``provider.chat_stream``.
        When provided, the last LLM call uses streaming; tool-call iterations
        still use ``chat_with_retry()`` (non-streaming).
    """

    if tool_defs is None:
        tool_defs = tool_registry.get_definitions()
    if not tool_defs:
        tool_defs = None  # provider expects None, not []

    _build = build_message or build_assistant_message
    _can_stream = on_content_delta and hasattr(provider, "chat_stream")

    result = ToolLoopResult(content=None, messages=messages)
    result.tools_available = tool_defs is not None
    iteration = 0
    response: LLMResponse | None = None

    while iteration < max_iterations:
        iteration += 1

        # ── Context breakdown before LLM call (only when debug enabled) ──
        if metadata and metadata.get("debug_context"):
            _ctx_total = 0
            _ctx_parts: list[str] = []
            for _ci, _cm in enumerate(messages):
                _role = _cm.get("role", "?")
                _name = _cm.get("name", "")
                _content = _cm.get("content") or ""
                _tc = _cm.get("tool_calls")
                _tcid = _cm.get("tool_call_id", "")
                if isinstance(_content, str):
                    _clen = len(_content)
                elif isinstance(_content, list):
                    _clen = sum(len(b.get("text", "")) for b in _content if isinstance(b, dict))
                else:
                    _clen = 0
                _ctx_total += _clen
                _preview = (_content[:40].replace("\n", " ") if isinstance(_content, str) else "")
                _extra = ""
                if _tc:
                    _names = [t.get("function", {}).get("name", "?") if isinstance(t, dict) else "?" for t in _tc]
                    _extra = f" tools=[{','.join(_names)}]"
                if _tcid:
                    _extra = f" tcid={_tcid[:9]}"
                _tag = f":{_name}" if _name else ""
                _ctx_parts.append(f"  [{_ci}] {_role}{_tag} {_clen:,}字{_extra} | {_preview}")
            logger.info(
                "tool_loop iter {} context ({}):\n{}\n  ── TOTAL: {:,} chars, {} msgs",
                iteration, model, "\n".join(_ctx_parts), _ctx_total, len(messages),
            )

        t0 = _time.time()

        # Use streaming when available for real-time text display.
        # _stream_call handles fallback to non-streaming if tool_calls
        # have empty names (Claude streaming bug).
        if _can_stream:
            response = await _stream_call(
                provider, messages, tool_defs, model, max_tokens, metadata,
                on_content_delta,
            )
        else:
            response = await provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=model,
                max_tokens=max_tokens,
                metadata=metadata,
            )

        latency = _time.time() - t0
        result.latency += latency

        # Token accounting
        usage = response.usage or {}
        result.token_usage["prompt"] += usage.get("prompt_tokens", 0)
        result.token_usage["completion"] += usage.get("completion_tokens", 0)
        result.token_usage["total"] += usage.get("total_tokens", 0)

        result.call_details.append({
            "iter": iteration,
            "latency": round(latency, 2),
            "tokens": dict(usage),
            "finish": response.finish_reason,
            "retry_log": response.retry_log,
            "tools": (
                [tc.name for tc in response.tool_calls]
                if response.has_tool_calls else []
            ),
        })

        raw_content = (response.content or "")[:100]
        logger.info(
            "tool_loop iter {}: finish={} tools={} content='{}'",
            iteration, response.finish_reason,
            bool(response.has_tool_calls), raw_content,
        )

        if response.has_tool_calls:
            # If content was streamed before tool calls were detected,
            # signal caller to clear the stale partial text from display
            if _can_stream and response.content and on_content_reset:
                await on_content_reset()

            # Surface intermediate thought
            thought = _strip_think(response.content)
            if thought and on_thought:
                await on_thought(thought)

            # Build and append assistant message with tool_calls
            tool_call_dicts = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ]
            messages.append(_build(
                response.content,
                tool_calls=tool_call_dicts,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))

            # Execute each tool call
            for tc in response.tool_calls:
                result.tools_used.append(tc.name)

                args_str = json.dumps(tc.arguments, ensure_ascii=False)
                logger.info("tool_loop: {}({})", tc.name, args_str[:200])

                if on_tool_start:
                    await on_tool_start(tc.name, tc.arguments)

                tc_start = _time.time()
                tc_error = None
                tool_result = None
                try:
                    tool_result = await tool_registry.execute(tc.name, tc.arguments)
                except Exception as exc:
                    tc_error = str(exc)
                    tool_result = f"Error: {exc}"
                    logger.error("tool_loop: {}() failed: {}", tc.name, tc_error[:200])
                tc_duration = round(_time.time() - tc_start, 2)

                # Record detail with timestamps
                result.tool_calls_detail.append({
                    "name": tc.name,
                    "args": args_str[:200],
                    "result_len": len(tool_result) if tool_result else 0,
                    "result_preview": (tool_result or "")[:150],
                    "timestamp": _time.strftime("%H:%M:%S"),
                    "duration": tc_duration,
                    "success": tc_error is None,
                    "error": tc_error[:200] if tc_error else None,
                    "iteration": iteration,
                })

                if on_tool_result:
                    await on_tool_result(tc.name, tc.id, tool_result)

                # Append tool result (truncated)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": (tool_result or "")[:result_max_chars],
                })
        else:
            # Text response — done
            raw_content = response.content
            content = _strip_think(raw_content)

            # Fallback: if content is empty but tools were used,
            # use the thought/content from the last tool-calling iteration.
            if not content and result.tools_used:
                # Search backward through messages for the last assistant message
                # that had content alongside tool_calls
                for msg in reversed(messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        fallback_text = _strip_think(msg["content"])
                        if fallback_text:
                            content = fallback_text
                            logger.info(
                                "tool_loop: using tool-iteration content as fallback "
                                "({} chars)", len(content),
                            )
                            break

            # Diagnostic: warn when model returns near-empty
            if not content and response.finish_reason == "stop":
                logger.warning(
                    "tool_loop: model returned empty content with stop "
                    "(raw={!r}, completion_tokens={})",
                    (raw_content or "")[:200],
                    usage.get("completion_tokens", "?"),
                )

            # Handle error responses
            if response.finish_reason == "error":
                logger.error("LLM returned error: {}", (content or "")[:200])
                result.content = content or "Error calling LLM."
                result.finish_reason = "error"
                result.status_code = getattr(response, "status_code", None)
                break


            # Append to messages (with reasoning if present)
            messages.append(_build(
                content,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))

            result.content = (
                clean_response(content) if clean_response and content
                else content
            )
            break

    # Max iterations fallback
    if result.content is None and iteration >= max_iterations:
        logger.warning("tool_loop: hit max iterations ({})", max_iterations)
        fallback = _strip_think(response.content if response else None)
        result.content = (
            clean_response(fallback) if clean_response and fallback
            else fallback
        )
        result.finish_reason = "max_iterations"

    result.iterations = iteration
    result.latency = round(result.latency, 2)
    return result


async def _stream_call(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    tool_defs: list[dict[str, Any]] | None,
    model: str,
    max_tokens: int,
    metadata: dict[str, Any] | None,
    on_content_delta: Callable[[str], Awaitable[None]],
) -> LLMResponse:
    """Call provider.chat_stream(), forwarding content deltas to the callback.

    Falls back to non-streaming chat_with_retry when:
    - Streaming fails with an exception
    - Stream produces an error response
    - Tool calls have empty names (Claude streaming bug)
    """
    response: LLMResponse | None = None
    try:
        async for item in provider.chat_stream(
            messages=messages,
            tools=tool_defs,
            model=model,
            max_tokens=max_tokens,
            metadata=metadata,
        ):
            if isinstance(item, str):
                await on_content_delta(item)
            elif isinstance(item, LLMResponse):
                response = item
    except Exception as e:
        logger.warning("Streaming failed, falling back to non-streaming: {}", e)
        response = None

    needs_fallback = False
    if response is None or response.finish_reason == "error":
        needs_fallback = True
    elif response.has_tool_calls:
        # Check for placeholder tool names (Claude streaming bug)
        has_unknown = any(tc.name == "_unknown_" for tc in response.tool_calls)
        if has_unknown:
            logger.info("_stream_call: tool calls have unknown names, retrying non-streaming")
            needs_fallback = True

    if needs_fallback:
        logger.info("_stream_call: using non-streaming fallback")
        response = await provider.chat_with_retry(
            messages=messages,
            tools=tool_defs,
            model=model,
            max_tokens=max_tokens,
            metadata=metadata,
        )

    return response


