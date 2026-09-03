"""Shared LLM + tool-calling loop.

Provides a single ``tool_loop()`` coroutine that both ``AgentLoop`` (core)
and ``GroupChatEngine`` (group chat) delegate to for the iterative
call-LLM → execute-tools → append-messages cycle.

Callers customise behaviour through optional callbacks rather than
subclassing, keeping the core loop logic in one place.
"""

from __future__ import annotations

import asyncio
import json
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.tools.registry import ToolRegistry
from nanobot.groupchat.history.result_processor import process_tool_result
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

    degenerate_repetition: bool = False
    """``True`` if the loop was terminated due to degenerate repetition."""

    finish_reason: str = "stop"
    """``"stop"`` for normal completion, ``"max_iterations"``, or ``"error"``."""

    status_code: int | None = None
    """HTTP status code on error (e.g. 429, 502)."""

    cost: float = 0.0
    """Accumulated cost across all LLM calls."""

    cache_tokens: int = 0
    """Accumulated cached prompt tokens."""

    provider_meta: list[dict[str, Any]] = field(default_factory=list)
    """Per-iteration provider metadata (model_id, provider, cost, etc.)."""


# ── Helpers ───────────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>")
_VENDOR_TAG_RE = re.compile(
    r"</?(?:minimax|anthropic|openai|meta):[^>]*>[\s\S]*?"
    r"(?:</(?:minimax|anthropic|openai|meta):[^>]*>|$)",
    re.I,
)
_TOOL_CALL_TAG_RE = re.compile(r"</?tool_call>[\s\S]*?(?:</tool_call>|$)", re.I)

# Tools that are idempotent and safe to dedup (same args => same result).
# chatroom_send is included to prevent agents from looping identical messages
# (e.g. sending the same greeting over and over, consuming all iterations).
_DEDUP_TOOLS = frozenset({
    "web_search", "web_fetch", "chatroom_send",
    "exec", "list_dir", "read_file",
})

# ── Degenerate repetition detection ────────────────────────────────────────

def _has_contiguous_repeat(text: str, min_repeat: int = 3) -> bool:
    """Detect if the same sentence/segment appears consecutively ≥ *min_repeat* times.

    Splits on sentence-ending punctuation or newlines, then checks for runs
    of identical (whitespace-normalised) segments.  O(n) — suitable for
    calling on every iteration without measurable overhead.
    """
    if not text:
        return False
    segments = re.split(r'[\n。！？.!?\u3002]', text)
    # Normalise and filter empty
    segs = [s.strip() for s in segments if s.strip()]
    if len(segs) < min_repeat:
        return False
    count = 1
    for i in range(1, len(segs)):
        if segs[i] == segs[i - 1]:
            count += 1
            if count >= min_repeat:
                return True
        else:
            count = 1
    return False


def _truncate_repeated_tail(text: str, min_repeat: int = 3) -> str:
    """Remove trailing contiguous repetitions from *text*.

    Keeps the first occurrence of each repeated segment and discards the
    duplicates that follow.  Returns the cleaned text.
    """
    if not text:
        return text
    segments = re.split(r'([\n。！？.!?\u3002])', text)
    # segments alternates: [content, delimiter, content, delimiter, ...]
    # Rebuild into (content, delimiter) pairs
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(segments):
        content = segments[i]
        delim = segments[i + 1] if i + 1 < len(segments) else ""
        # Skip trailing empty pairs (produced by re.split when text ends with delimiter)
        if content or delim:
            pairs.append((content, delim))
        i += 2

    if len(pairs) < min_repeat:
        return text

    # Find the longest trailing run of identical (content, delim) pairs
    last_unique_idx = len(pairs) - 1
    for i in range(len(pairs) - 2, -1, -1):
        if pairs[i] == pairs[i + 1]:
            last_unique_idx = i
        else:
            break

    # Check if the run is long enough
    run_len = len(pairs) - last_unique_idx
    if run_len >= min_repeat:
        # Keep up to and including the first occurrence of the repeated segment
        keep_until = last_unique_idx + 1
        return "".join(c + d for c, d in pairs[:keep_until])

    return text


# Shell redirections to strip when normalizing exec commands for dedup.
_SHELL_REDIR_RE = re.compile(
    r'\s*(?:2>&1|2>/dev/null|>/dev/null|2>\s*\S+|&>\s*\S+)\s*',
)


def _normalize_dedup_args(name: str, arguments: dict) -> str:
    """Build a normalized dedup key for a tool call.

    For ``exec``, strip trailing shell redirections (e.g. ``2>&1``) so that
    ``git clone foo 2>&1`` and ``git clone foo`` are treated as identical.
    """
    if name == "exec" and "command" in arguments:
        norm = dict(arguments)
        norm["command"] = _SHELL_REDIR_RE.sub("", norm["command"]).strip()
        return f"{name}:{json.dumps(norm, sort_keys=True, ensure_ascii=False)}"
    return f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"


def _strip_think(text: str | None) -> str | None:
    """Remove ``<think>…</think>``, vendor XML tags, and leaked tool_call tags."""
    if not text:
        return None
    text = _THINK_RE.sub("", text)
    text = _VENDOR_TAG_RE.sub("", text)
    text = _TOOL_CALL_TAG_RE.sub("", text)
    return text.strip() or None


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
    reasoning_effort: str | None = None,
    # Per-caller sampling params (e.g. per-agent hyperparams), merged on top
    # of the provider defaults inside _build_kwargs. Passed explicitly so
    # concurrent callers sharing one provider never mutate shared state.
    sampling_override: dict[str, Any] | None = None,
    # ── Callbacks ──
    on_tool_start: Callable[..., Awaitable[None]] | None = None,
    on_tool_result: Callable[[str, str, str], Awaitable[None]] | None = None,
    on_iteration_usage: Callable[[dict], Awaitable[None]] | None = None,
    on_thought: Callable[[str], Awaitable[None]] | None = None,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_content_reset: Callable[[], Awaitable[None]] | None = None,
    clean_response: Callable[[str], str] | None = None,
    build_message: Callable[..., dict[str, Any]] | None = None,
    result_max_chars: int | None = None,
    call_timeout: float | None = None,
    # ── Cooperative interrupt ──
    interrupt_event: "asyncio.Event | None" = None,
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

    # ── Resolve dynamic defaults ──
    if result_max_chars is None:
        try:
            from nanobot.groupchat.history.history_settings import get_tool_result_max_chars
            result_max_chars = get_tool_result_max_chars()
        except Exception:
            result_max_chars = 64_000

    if tool_defs is None:
        tool_defs = tool_registry.get_definitions()
    if not tool_defs:
        tool_defs = None  # provider expects None, not []

    # Second-layer permission: set of allowed tool names for execution guard
    _allowed_tools: set[str] | None = None
    if tool_defs is not None:
        _allowed_tools = {d.get("function", {}).get("name", "") for d in tool_defs}

    _build = build_message or build_assistant_message
    _can_stream = on_content_delta and hasattr(provider, "chat_stream")

    result = ToolLoopResult(content=None, messages=messages)
    result.tools_available = tool_defs is not None
    iteration = 0
    response: LLMResponse | None = None
    _seen_calls: dict[str, str] = {}  # "name:args_json" -> cached result

    # Resolve context_window_tokens for pruning (from history_settings or default)
    try:
        from nanobot.groupchat.history.history_settings import get_context_window_tokens
        _ctx_window = get_context_window_tokens()
    except Exception:
        _ctx_window = 200_000

    while iteration < max_iterations:
        iteration += 1

        # ── Checkpoint 1: cooperative interrupt check (before LLM call) ──
        # Checked at the top of every iteration so we exit as quickly as
        # possible when another agent has sent an urgent message.
        if interrupt_event is not None and interrupt_event.is_set():
            logger.info(
                "tool_loop: ⚡ interrupt detected before LLM call (iter {})", iteration
            )
            result.finish_reason = "interrupted"
            break

        # ── Context pruning: trim old tool results to save tokens ──
        # On iteration 2+, prune old tool results before sending to LLM.
        # This is a read-only operation — the original messages list is not
        # mutated, so new tool results continue to be appended correctly.
        if iteration > 1:
            from nanobot.groupchat.history.tool_pruning import prune_messages
            llm_messages = prune_messages(messages, _ctx_window)
        else:
            llm_messages = messages

        # Drop orphan tool messages (role=tool without tool_call_id) that would
        # cause provider API errors. These can appear from stale cache, history
        # reloads, or interrupted tool batches.
        llm_messages = [
            m for m in llm_messages
            if not (m.get("role") == "tool" and not m.get("tool_call_id"))
        ]

        # ── Context breakdown before LLM call ──
        # Always log at DEBUG level; also log at INFO when debug_context is set
        _ctx_total = 0
        _ctx_parts: list[str] = []
        for _ci, _cm in enumerate(llm_messages):
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
            # Full content for complete logging
            if isinstance(_content, str):
                _preview = _content.replace("\n", "\\n")
            elif isinstance(_content, list):
                _preview = " ".join(b.get("text", "") for b in _content if isinstance(b, dict)).replace("\n", "\\n")
            else:
                _preview = str(_content)
            _extra = ""
            if _tc:
                _names = [t.get("function", {}).get("name", "?") if isinstance(t, dict) else "?" for t in _tc]
                _extra = f" tools=[{','.join(_names)}]"
            if _tcid:
                _extra = f" tcid={_tcid}"
            _tag = f":{_name}" if _name else ""
            _ctx_parts.append(f"  [{_ci}] {_role}{_tag} {_clen:,}字{_extra} | {_preview}")
        _pruned_note = f" (pruned from {len(messages)})" if len(llm_messages) != len(messages) else ""
        _log_fn = logger.info if (metadata and metadata.get("debug_context")) else logger.debug
        _log_fn(
            "tool_loop iter {} context ({}):\n{}\n  ── TOTAL: {:,} chars, {} msgs{}",
            iteration, model, "\n".join(_ctx_parts), _ctx_total, len(llm_messages), _pruned_note,
        )

        t0 = _time.time()

        # On the last iteration, if tools were already used, skip passing
        # tool definitions to force the model to generate a text response
        # instead of calling more tools (avoids all-tool-call exhaustion).
        iter_tool_defs = tool_defs
        if iteration == max_iterations and result.tools_used:
            logger.info(
                "tool_loop iter {}: last iteration, dropping tools to force text response",
                iteration,
            )
            iter_tool_defs = None

        # Use streaming when available for real-time text display.
        # _stream_call handles fallback to non-streaming if tool_calls
        # have empty names (Claude streaming bug).
        try:
            if _can_stream:
                _coro = _stream_call(
                    provider, llm_messages, iter_tool_defs, model, max_tokens, metadata,
                    on_content_delta, reasoning_effort=reasoning_effort,
                    sampling_override=sampling_override,
                )
            else:
                _coro = provider.chat_with_retry(
                    messages=llm_messages,
                    tools=iter_tool_defs,
                    model=model,
                    max_tokens=max_tokens,
                    metadata=metadata,
                    reasoning_effort=reasoning_effort,
                    sampling_override=sampling_override,
                )
                
            if interrupt_event is not None:
                # Race the LLM call against the cooperative interrupt
                async def _wait_interrupt():
                    await interrupt_event.wait()
                    
                intr_task = asyncio.create_task(_wait_interrupt())
                llm_task = asyncio.create_task(_coro)
                
                try:
                    done, pending = await asyncio.wait(
                        [intr_task, llm_task],
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=call_timeout if call_timeout and call_timeout > 0 else None,
                    )
                except asyncio.CancelledError:
                    # Clean up tasks if the main loop is cancelled (e.g. Leader end_discussion)
                    intr_task.cancel()
                    llm_task.cancel()
                    raise
                    
                if intr_task in done:
                    # Interrupted during LLM call!
                    llm_task.cancel()
                    try:
                        await llm_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    
                    logger.info(
                        "tool_loop: ⚡ interrupt detected DURING LLM call (iter {})", iteration
                    )
                    result.finish_reason = "interrupted"
                    break
                elif llm_task in done:
                    # LLM finished successfully or with its own error
                    intr_task.cancel()
                    response = llm_task.result()
                else:
                    # Timeout
                    intr_task.cancel()
                    llm_task.cancel()
                    raise asyncio.TimeoutError()
            else:
                # Normal path without interrupt support
                if call_timeout and call_timeout > 0:
                    response = await asyncio.wait_for(_coro, timeout=call_timeout)
                else:
                    response = await _coro
                    
        except asyncio.TimeoutError:
            latency = _time.time() - t0
            result.latency += latency
            _agent = (metadata or {}).get("log_agent", "?")
            logger.warning(
                "tool_loop: LLM call timed out ({:.1f}s) on iter {} — model={} agent={}",
                latency, iteration, model, _agent,
            )
            result.finish_reason = "timeout"
            break

        latency = _time.time() - t0
        result.latency += latency

        # Token accounting
        usage = response.usage or {}
        result.token_usage["prompt"] += usage.get("prompt_tokens", 0)
        result.token_usage["completion"] += usage.get("completion_tokens", 0)
        result.token_usage["total"] += usage.get("total_tokens", 0)
        if response.cost:
            result.cost += response.cost
        result.cache_tokens += response.cache_tokens
        if response.provider_meta:
            result.provider_meta.append(response.provider_meta)

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

        raw_content = response.content or ""
        logger.info(
            "tool_loop iter {}: finish={} tools={} content='{}'",
            iteration, response.finish_reason,
            bool(response.has_tool_calls), raw_content,
        )

        # ── Degenerate repetition guard ──────────────────────────────────
        # Detect when the model falls into a self-reinforcing repetition
        # loop (same sentence repeated ≥3 times).  Break immediately to
        # avoid burning iterations / tokens on useless output.
        _check_text = raw_content
        # Also check reasoning/thinking content if present
        if not _check_text and hasattr(response, "reasoning_content") and response.reasoning_content:
            _check_text = response.reasoning_content
        if _has_contiguous_repeat(_check_text):
            logger.warning(
                "tool_loop iter %d: degenerate repetition detected, breaking loop",
                iteration,
            )
            # Keep whatever non-repeated content we have so far
            _non_repeated = _truncate_repeated_tail(raw_content)
            if _non_repeated != raw_content:
                raw_content = _non_repeated
            result.content = raw_content
            result.finish_reason = "degenerate_repetition"
            break

        if response.has_tool_calls:
            # Notify per-iteration token usage (before tool execution)
            # Enrich usage with cost and cache_tokens so display callbacks
            # can show a complete token suffix.
            if on_iteration_usage and usage:
                enriched = dict(usage)
                if response.cost:
                    enriched["cost"] = response.cost
                if response.cache_tokens:
                    enriched["cache_tokens"] = response.cache_tokens
                await on_iteration_usage(enriched)
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

            # ── Phase 1: Sequential pre-check (dedup, permission, progress) ──
            pending: list[tuple] = []  # (tc, args_str, dedup_key)
            _batch_dedup_keys: set[str] = set()  # track dedup keys within this batch
            for tc in response.tool_calls:
                result.tools_used.append(tc.name)

                args_str = json.dumps(tc.arguments, ensure_ascii=False)
                logger.info("tool_loop: {}({})", tc.name, args_str)

                # Dedup check BEFORE display (so dupes are silent)
                dedup_key = None
                if tc.name in _DEDUP_TOOLS:
                    dedup_key = _normalize_dedup_args(tc.name, tc.arguments)
                    # Check both cross-iteration cache AND within-batch duplicates
                    if dedup_key in _seen_calls or dedup_key in _batch_dedup_keys:
                        tool_result = (
                            (_seen_calls.get(dedup_key, "") or "")
                            + "\n\n[DUPLICATE] 你已经用完全相同的参数调用过此工具，结果与上次一致。"
                            "请使用不同的搜索词，或直接基于已有结果回答用户的问题。"
                        )
                        logger.warning(
                            "tool_loop: DUPLICATE call skipped: {}({})",
                            tc.name, args_str[:100],
                        )
                        result.tool_calls_detail.append({
                            "name": tc.name,
                            "args": args_str[:200],
                            "result_len": len(tool_result),
                            "result_preview": "[DUPLICATE]",
                            "timestamp": _time.strftime("%H:%M:%S"),
                            "duration": 0.0,
                            "success": True,
                            "error": None,
                            "iteration": iteration,
                            "duplicate": True,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result[:result_max_chars],
                        })
                        continue
                    _batch_dedup_keys.add(dedup_key)

                # Permission guard: block unauthorized tool calls
                if _allowed_tools is not None and tc.name not in _allowed_tools:
                    tool_result = (
                        f"BLOCKED: 你没有 {tc.name} 的使用权限。"
                        f"可用工具: {', '.join(sorted(_allowed_tools))}"
                    )
                    logger.warning("tool_loop: BLOCKED unauthorized call: {}", tc.name)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result,
                    })
                    continue

                if on_tool_start:
                    await on_tool_start(tc.name, tc.arguments, tool_call_id=tc.id)

                pending.append((tc, args_str, dedup_key))

            # ── Checkpoint 2.5: interrupt check before tool execution ──
            # After LLM has decided on tools (expending one LLM call) but
            # BEFORE executing them.  Prevents a long tool batch (web_search ×3,
            # exec, fetch…) from blocking the interrupt for another 30-90s.
            if interrupt_event is not None and interrupt_event.is_set():
                logger.info(
                    "tool_loop: ⚡ interrupt detected before tool batch (iter {}, {} tools pending)",
                    iteration, len(pending),
                )
                result.finish_reason = "interrupted"
                break

            # ── Phase 2: Parallel execution via asyncio.gather ──
            async def _exec_one(tc_inner):
                t0 = _time.time()
                try:
                    res = await tool_registry.execute(tc_inner.name, tc_inner.arguments)
                    return res, None, round(_time.time() - t0, 2)
                except Exception as exc:
                    err = str(exc)
                    logger.error("tool_loop: {}() failed: {}", tc_inner.name, err[:200])
                    return f"Error: {exc}", err, round(_time.time() - t0, 2)

            if pending:
                exec_results = await asyncio.gather(
                    *(_exec_one(tc) for tc, _, _ in pending),
                    return_exceptions=True,
                )

                # ── Phase 3: Sequential post-process ──
                for (tc, args_str, dedup_key), raw in zip(pending, exec_results):
                    # Handle unexpected gather exceptions (e.g. BaseException)
                    if isinstance(raw, BaseException):
                        tool_result = f"Error: {type(raw).__name__}: {raw}"
                        tc_error = str(raw)
                        tc_duration = 0.0
                    else:
                        tool_result, tc_error, tc_duration = raw

                    # Log full tool result
                    if isinstance(tool_result, list):
                        _log_result = json.dumps(tool_result, ensure_ascii=False)
                    else:
                        _log_result = tool_result or ""
                    logger.info(
                        "tool_loop: {}() result ({}c, {:.2f}s): {}",
                        tc.name, len(_log_result), tc_duration, _log_result,
                    )

                    # Cache result for dedup
                    if dedup_key is not None and tc_error is None:
                        if isinstance(tool_result, list):
                            _seen_calls[dedup_key] = json.dumps(tool_result, ensure_ascii=False)[:result_max_chars]
                        else:
                            _seen_calls[dedup_key] = (tool_result or "")[:result_max_chars]

                    # Record detail with timestamps
                    if isinstance(tool_result, list):
                        _result_str = json.dumps(tool_result, ensure_ascii=False)
                    else:
                        _result_str = tool_result or ""
                    result.tool_calls_detail.append({
                        "name": tc.name,
                        "args": args_str[:200],
                        "result_len": len(_result_str),
                        "result_preview": _result_str[:4000],
                        "timestamp": _time.strftime("%H:%M:%S"),
                        "duration": tc_duration,
                        "success": tc_error is None,
                        "error": tc_error[:200] if tc_error else None,
                        "iteration": iteration,
                    })

                    if on_tool_result:
                        # Normalize list results to str before passing to callback
                        # (broadcast_view.on_tool_result calls .startswith() which fails on list)
                        _callback_result = (
                            json.dumps(tool_result, ensure_ascii=False)
                            if isinstance(tool_result, list)
                            else (tool_result or "")
                        )
                        await on_tool_result(tc.name, tc.id, _callback_result)

                    tool_content = process_tool_result(
                        content=tool_result,
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_content,
                    })
                    from nanobot.groupchat.orchestra.events import get_bus
                    get_bus().emit_nowait(
                        "tool:result",
                        tool=tc.name, ok=not isinstance(tool_result, BaseException),
                        chars=len(tool_content) if isinstance(tool_content, str) else 0,
                    )

                # ── Phase 3b: Handle forget tool — delete matched tool call+result pairs ──
                _forget_ids: set[str] = set()
                _forget_names: list[str] = []
                for (tc, args_str, dedup_key), raw in zip(pending, exec_results):
                    if isinstance(raw, BaseException):
                        continue
                    tool_result, _, _ = raw
                    if isinstance(tool_result, str) and tool_result.startswith("__FORGET__:"):
                        try:
                            import json as _json
                            info = _json.loads(tool_result[len("__FORGET__:"):])
                            _forget_ids.update(info.get("tool_call_ids", []))
                            _forget_names.extend(info.get("names", []))
                        except Exception:
                            pass

                if _forget_ids:
                    # Collect forget tool's own IDs separately for residue cleanup.
                    _forget_self_ids: set[str] = set()
                    # Also build a map: tool_call_id → name for descriptive replacement
                    _id_to_name: dict[str, str] = {}
                    for (tc, args_str, dedup_key), raw in zip(pending, exec_results):
                        if isinstance(raw, BaseException):
                            continue
                        tool_result, _, _ = raw
                        if isinstance(tool_result, str) and tool_result.startswith("__FORGET__:"):
                            _forget_self_ids.add(tc.id)
                            try:
                                import json as _json
                                info = _json.loads(tool_result[len("__FORGET__:"):])
                                for tid, nm in zip(info.get("tool_call_ids", []),
                                                    info.get("names", [])):
                                    _id_to_name[tid] = nm
                            except Exception:
                                pass

                    # ── Actually excise target tool results and clean assistant tool_calls ──
                    # This ensures forgotten items are truly removed from the shared history
                    # used by compression (maybe_compress) and context building (history_to_messages).
                    # The "know" trace is kept only in the forget tool's own result summary.
                    # Previously this was replace+mark, which left entries in the list and
                    # could still affect positional middle selection or prompt reconstruction.
                    messages[:] = [m for m in messages if not (m.get("role") == "tool" and m.get("tool_call_id") in _forget_ids and m.get("tool_call_id") not in _forget_self_ids)]

                    for m in messages:
                        if m.get("role") == "assistant" and m.get("tool_calls"):
                            m["tool_calls"] = [tc for tc in m["tool_calls"] if tc.get("id") not in _forget_ids]

                    # ── Replace forget tool's own result with clear summary ──
                    # (keep entry so agent knows forget was already called)
                    forget_msg = ", ".join(f"{n}×{c}" for n, c in Counter(_forget_names).items())
                    for m in messages:
                        if m.get("role") == "tool" and m.get("tool_call_id") in _forget_self_ids:
                            m["content"] = f"✓ forgot: {forget_msg}"

                    logger.info(
                        "tool_loop: forget excised {} tool call(s): {} from history",
                        len(_forget_ids), ", ".join(_forget_names),
                    )

                    result.forgotten_tool_call_ids.extend(
                        sorted(tid for tid in _forget_ids if tid)
                    )

                    if forget_tool is not None and hasattr(forget_tool, "_ctx"):
                        forget_tool._ctx.setdefault("_forgot_ids", set()).update(_forget_ids)

                # ── Checkpoint 2.5: end_discussion early exit (BEFORE interrupt check) ──
                # Must be checked before the interrupt checkpoint below, because
                # mark_discussion_ended() sets ALL interrupt events (including the
                # caller's own) to wake mid-turn agents.  If the interrupt check
                # runs first, the agent that called end_discussion would see its
                # own phantom interrupt and re-enter the loop instead of exiting
                # cleanly with finish_reason="end_discussion".
                if "end_discussion" in result.tools_used:
                    logger.info(
                        "tool_loop: end_discussion detected after tool batch (iter {}), breaking", iteration
                    )
                    if not result.content and raw_content:
                        result.content = raw_content
                    result.finish_reason = "end_discussion"
                    break

                # ── Checkpoint 2: cooperative interrupt check (after tool batch) ──
                # Checked after all tools in this iteration have finished so
                # we don't interrupt mid-batch (keeps tool/message ledger clean).
                if interrupt_event is not None and interrupt_event.is_set():
                    logger.info(
                        "tool_loop: ⚡ interrupt detected after tool batch (iter {})", iteration
                    )
                    result.finish_reason = "interrupted"
                    break

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

            # Handle error responses — retry once via non-streaming fallback
            if response.finish_reason == "error":
                logger.error("LLM returned error: {}", (content or "")[:200])
                # Retry once: same params, non-streaming guaranteed
                try:
                    retried = await provider.chat_with_retry(
                        messages=messages,
                        tools=tool_defs,
                        model=model,
                        max_tokens=max_tokens,
                        metadata=metadata,
                        reasoning_effort=reasoning_effort,
                        sampling_override=sampling_override,
                    )
                    if retried.finish_reason != "error":
                        response = retried
                        raw_content = response.content
                        content = _strip_think(raw_content)
                        logger.info("tool_loop: error-retry succeeded")
                    else:
                        raise RuntimeError(retried.content or "Retry also errored")
                except Exception as retry_err:
                    logger.error("tool_loop: error-retry also failed: {}", retry_err)
                    result.content = content or f"Error calling LLM after retry: {retry_err}"
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
        # Clean up leaked tool call tags (Claude sometimes hallucinates
        # <tool_call>...</tool_call> in text when tools are stripped)
        if fallback:
            fallback = re.sub(
                r'</?tool_call>.*', '', fallback, flags=re.DOTALL
            ).strip() or fallback
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
    *,
    reasoning_effort: str | None = None,
    sampling_override: dict[str, Any] | None = None,
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
            sampling_override=sampling_override,
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
            # Check if arguments are also empty — if so, the API proxy
            # can't stream tool_calls at all. Don't retry (API is
            # non-deterministic and often returns empty on retry).
            # Instead, return the content as a text response.
            all_args_empty = all(not tc.arguments for tc in response.tool_calls)
            if all_args_empty:
                logger.warning(
                    "_stream_call: tool calls have no names AND no args, "
                    "treating as text response (content={} chars)",
                    len(response.content or ""),
                )
                # Convert to text-only response
                response = LLMResponse(
                    content=response.content,
                    tool_calls=[],
                    finish_reason="stop",
                    usage=response.usage,
                )
            else:
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
            reasoning_effort=reasoning_effort,
            sampling_override=sampling_override,
        )

    return response


