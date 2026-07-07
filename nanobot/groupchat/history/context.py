"""HistoryContext — single source of truth for conversation history.

Owns the message list and all related operations:
  - add_message     : append + enforce message-count & char-budget limits
  - maybe_compress  : AI summarize (or mechanically compress) the middle
                      region on overflow (token-aware via tiktoken estimator)
  - clear / format  : utility helpers

Engine and Broadcast delegate to this class instead of maintaining
``self._history`` directly.  Persistence is still delegated to
``GroupChatState.save_message()``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.groupchat.history.message_converter import (
    age_tool_log,
    build_compress_message,
    has_tool_log,
    trim_sender_history,
)

if TYPE_CHECKING:
    from nanobot.groupchat.history.persistence import GroupChatState

# Prefixes of compression artefacts injected into history. Messages whose
# content starts with one of these (after lstrip) are protected on the next
# compression pass so multi-pass compression doesn't drop or dilute them.
#   [早期对话摘要 — AI summary block (this module)
#   [早期对话压缩 — mechanical compress block (message_converter.build_compress_message)
_SUMMARY_PREFIXES = ("[早期对话摘要", "[早期对话压缩")
_SUMMARY_PREFIX = _SUMMARY_PREFIXES[0]

# Fallbacks used when history_settings cannot be imported at runtime. These
# intentionally match the defaults in history_settings._DEFAULTS so that a
# settings-load failure does not silently change behaviour (the previous code
# fell back to limit=150, which differed from the configured default of 50).
_FALLBACK_MAX_MESSAGES = 50
_FALLBACK_KEEP_USERS = True


def is_compact_summary(msg: dict) -> bool:
    """Whether *msg* is a compaction boundary/summary artefact.

    Structured flag first (set on all new summary/compress blocks), with a
    legacy string-prefix fallback so messages persisted before the flag
    existed — or restored via engines that strip extra keys — are still
    recognised and protected on the next compression pass.
    """
    if msg.get("is_compact_summary"):
        return True
    content = msg.get("content")
    return isinstance(content, str) and content.lstrip().startswith(_SUMMARY_PREFIXES)


def find_last_compact_boundary(messages: list[dict]) -> int:
    """Index of the last compact-summary message, or -1 if none."""
    for i in range(len(messages) - 1, -1, -1):
        if is_compact_summary(messages[i]):
            return i
    return -1


class HistoryContext:
    """Encapsulates the shared conversation history for a group chat session.

    Parameters
    ----------
    state:
        The ``GroupChatState`` persistence layer; used to persist each
        message and the current history snapshot to disk.
    provider:
        The LLM provider used for AI summarisation.  May be ``None`` when
        summarisation is disabled.
    """

    def __init__(
        self,
        state: "GroupChatState",
        provider: Any = None,
    ) -> None:
        self._state = state
        self._provider = provider
        self.messages: list[dict[str, str]] = []
        self._compress_lock = asyncio.Lock()
        # True while maybe_compress is in flight (including its LLM await).
        # add_message then appends WITHOUT trimming, so that the compression
        # rebuild can rely on `self.messages[snapshot_len:]` being exactly
        # the messages appended during the await (trimming would replace the
        # list and shift indices, losing those appends).
        self._compress_active = False

    # ── Compatibility shim: allow engine._history to keep working ─────────
    # We expose the messages list directly as a public attribute so that
    # code still referencing ``engine._history`` can be updated gradually.
    # TODO: remove once all callers are migrated.

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _find_head_indices(history: list[dict], keep_all_users: bool = False) -> set[int]:
        """Return indices of head-protected messages.

        Always protects index 0 (system prompt).  If *keep_all_users* is True,
        protects **all** user messages; otherwise only the first user message.
        """
        protected = {0}
        for i, msg in enumerate(history):
            if msg.get("sender") in ("User", "user", "用户"):
                protected.add(i)
                if not keep_all_users:
                    break
        return protected

    # ── Public API ────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.messages)

    def __bool__(self) -> bool:
        return bool(self.messages)

    def __iter__(self):
        return iter(self.messages)

    def __getitem__(self, idx):
        return self.messages[idx]

    def clear(self) -> None:
        """Wipe the entire history."""
        self.messages.clear()

    def format(self) -> str:
        """Format history as a single readable string."""
        return "\n\n".join(
            f"[{m['sender']}]: {m['content']}" for m in self.messages
        )

    def add_message(self, sender: str, content: str) -> None:
        """Append a message and enforce message-count and token-budget limits.

        Head-protection guarantees that the very first message and the first
        user message are never evicted during trimming.

        Token trimming uses tiktoken-based estimation so that trimming
        decisions reflect actual LLM token cost rather than raw character count.
        A separate char-based trim still runs in history_to_messages as a safety net.

        While ``maybe_compress`` is in flight, trimming is skipped (append +
        persist only) so the compression rebuild can safely re-attach messages
        appended during its LLM await. The next add_message re-trims.
        """
        # ── Cross-turn repetition guard (observational, log-only) ──
        # Warn when an agent's new message is near-identical to its own
        # previous message (no new info this turn). Does NOT mutate content
        # — see repetition.py for why stubbing would be counterproductive.
        if sender not in ("系统", "User", "user", "用户") and isinstance(content, str):
            try:
                from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
                    cross_turn_repeat_guard,
                    cross_turn_repeat_ratio,
                )
                from nanobot.groupchat.history.repetition import (  # noqa: PLC0415
                    is_cross_turn_repeat,
                )
                if cross_turn_repeat_guard():
                    _thr = cross_turn_repeat_ratio()
                    for m in reversed(self.messages):
                        if m.get("sender") == sender:
                            prev = m.get("content", "") or ""
                            if isinstance(prev, str):
                                repeated, score = is_cross_turn_repeat(content, prev, _thr)
                                if repeated:
                                    logger.warning(
                                        "HistoryContext: cross-turn repeat by {} "
                                        "(similarity {:.0%}, new {}字 vs prev {}字) "
                                        "— no new info this turn",
                                        sender, score, len(content), len(prev),
                                    )
                            break
            except Exception as e:
                logger.debug(
                    "HistoryContext: cross-turn repeat check skipped: {}", e,
                )

        self.messages.append({"sender": sender, "content": content})

        if self._compress_active:
            self._state.save_message(sender, content, self.messages)
            return

        try:
            from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
                max_messages,
                keep_user_messages,
            )
            limit = max_messages()
            _keep_users = keep_user_messages()
        except Exception as e:
            # Fall back to defaults that match history_settings._DEFAULTS so a
            # settings-load failure does not silently change trim limits.
            logger.warning("HistoryContext.add_message: history_settings unavailable ({}); using fallbacks", e)
            limit = _FALLBACK_MAX_MESSAGES
            _keep_users = _FALLBACK_KEEP_USERS

        # ── Pre-identify protected head before any trimming ──
        head_indices = self._find_head_indices(self.messages, keep_all_users=_keep_users)
        head_msgs = [self.messages[i] for i in sorted(head_indices)]

        # Step 1: message-count limit — keep most-recent N, always keep head
        if len(self.messages) > limit:
            tail = self.messages[-limit:]
            tail_ids = {id(m) for m in tail}
            extra_head = [m for m in head_msgs if id(m) not in tail_ids]
            self.messages = extra_head + tail

        # ── Step 2: token-budget trimming ──
        # Uses tiktoken-based estimation (estimate_message_tokens) so
        # trimming decisions reflect actual LLM token cost.
        # Budget = context_budget_ratio() of context_window (default 0.65),
        # leaving headroom for system prompt, tools, etc. Falls back to
        # char/4 if estimator unavailable. Head (index 0 + user messages)
        # is always protected. Note: max_context_chars is still enforced
        # separately in history_to_messages -> trim_llm_messages as a second
        # safety net.
        try:
            from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
                context_budget_ratio,
                get_context_window_tokens,
            )
            from nanobot.utils.helpers import estimate_message_tokens as _est_tok

            token_window = get_context_window_tokens()
            if token_window > 0:
                token_budget = int(token_window * context_budget_ratio())

                # ── Early-exit: skip the expensive tiktoken pass + tiered trim
                # when we're clearly under budget. Char count is a cheap proxy.
                # Worst realistic case for cl100k is CJK at ~1 token/char, so
                # `total_chars <= token_budget` implies tokens are (near) under
                # budget. Only past that do we pay for per-message tiktoken
                # encoding + trim_sender_history. maybe_compress (real tiktoken
                # trigger) and trim_llm_messages (char safety net) still guard
                # the pathological all-rare-unicode case downstream.
                total_chars = sum(len(m.get("content", "")) for m in self.messages)
                if total_chars > token_budget:
                    def _as_llm(m):
                        role = "user" if m.get("sender") in ("User", "user", "用户") else "assistant"
                        return {"role": role, "content": m.get("content", "")}

                    head_indices = self._find_head_indices(self.messages, keep_all_users=_keep_users)

                    def _tok_len(m: dict) -> int:
                        try:
                            return int(_est_tok(_as_llm(m)) or 0)
                        except Exception:
                            return len(m.get("content", "")) // 4 + 4

                    self.messages = trim_sender_history(
                        self.messages,
                        token_budget,
                        protected_indices=head_indices,
                        length_fn=_tok_len,
                    )
        except Exception:
            # Estimator or settings not available; rely on previous char/count logic
            pass

        self._state.save_message(sender, content, self.messages)

    def microcompact(self) -> None:
        """Cheap, no-LLM pre-pass: age old tool-log blocks before compression.

        Ported from Claude Code's microcompact layer (the second tier of
        context management, which runs every turn *before* the autocompact
        threshold). In nanobot's sender-based history, tool results live
        inside ``<previous_tool_calls>…</previous_tool_calls>`` (or legacy
        ``[工具调用记录]``) blocks appended to assistant message content.
        ``age_tool_log`` collapses each such block to its first-line summary.

        This pre-pass keeps the in-memory history lean so that the expensive
        AI summarisation in :meth:`maybe_compress` fires later (and its
        summary input is smaller). It is:

        - **Sync / no LLM / no lock**: runs to completion before
          :meth:`maybe_compress` acquires ``_compress_lock``; single-threaded
          asyncio means ``add_message`` cannot interleave here.
        - **Idempotent**: ``age_tool_log`` on already-aged content returns the
          same bytes, so re-running every round is safe and cheap.
        - **Tail- and head-protected**: only messages older than the last
          ``compression_keep_recent`` are touched; index 0 (system prompt)
          and user messages are always skipped.
        - **Skipped while a compression is in flight** (``_compress_active``)
          to avoid mutating during a rebuild.

        Mutates ``msg["content"]`` in place — this is the intended permanent
        change (mirrors Claude Code's permanent microcompact). The next
        ``_persist_chat_state`` persists the aged content.
        """
        if self._compress_active:
            return
        try:
            from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
                compression_keep_recent,
            )
            keep_recent = compression_keep_recent()
        except Exception as e:
            logger.warning(
                "HistoryContext.microcompact: history_settings unavailable ({}); "
                "using fallback keep_recent=6", e,
            )
            keep_recent = 6

        total = len(self.messages)
        # Only the region before the protected tail is eligible for aging.
        # ``max(0, …)`` keeps the slice valid when history is shorter than
        # keep_recent (then eligible_end <= 0 and the loop is a no-op).
        eligible_end = max(0, total - keep_recent)
        changed = 0
        for i in range(eligible_end):
            msg = self.messages[i]
            # Head protection: never touch the system prompt or any user msg.
            if i == 0 or msg.get("sender") in ("User", "user", "用户"):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not has_tool_log(content):
                continue
            aged = age_tool_log(content)
            if aged != content:
                msg["content"] = aged
                changed += 1
        if changed:
            logger.debug(
                "HistoryContext.microcompact: aged tool-log blocks in {}/{} eligible messages",
                changed, eligible_end,
            )

    async def maybe_compress(self) -> None:
        """Compress the middle section of history when it approaches the limit.

        Head-tail protection always runs.  AI summarisation is gated by
        ``summarize_enabled``; if disabled, the middle region is mechanically
        compressed via ``build_compress_message`` (never silently dropped).

        Protected by an async lock to prevent concurrent corruption.

        Race-safety: snapshots ``self.messages`` before any ``await``. Messages
        appended by ``add_message`` during the (long) LLM call are preserved
        by appending ``self.messages[snapshot_len:]`` after the rebuild.
        """
        async with self._compress_lock:
            from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
                max_messages,
                history_summarize_enabled,
                summarize_model,
                compress_ratio,
                compress_max_summary_tokens,
                compression_keep_recent,
                keep_user_messages,
                token_trigger_ratio,
                compress_fallback_chars,
            )

            limit = max_messages()
            ratio = compress_ratio()
            # Token-pressure trigger ratio (default 0.55): when token usage
            # crosses this fraction of the context window, compression fires
            # even if message-count ratio is still under `ratio`. Replaces the
            # previously hardcoded 0.55 literal so it is now configurable and
            # no longer a silent override of compress_ratio.
            tok_trigger = token_trigger_ratio()

            # Token-aware trigger (new): use real estimator when available so that
            # long individual messages or tool output bloat trigger compression
            # even if message count is still under the old limit.
            current_tok = 0
            token_ratio = 0.0
            try:
                from nanobot.groupchat.history.history_settings import get_context_window_tokens
                from nanobot.utils.helpers import estimate_message_tokens
                _est = estimate_message_tokens
                token_window = get_context_window_tokens()
                # Map our sender-based history items to something the estimator understands
                def _as_llm_msg(m):
                    role = "user" if m.get("sender") in ("User", "user", "用户") else "assistant"
                    return {"role": role, "content": m.get("content", "")}
                current_tok = sum(int(_est(_as_llm_msg(m)) or 0) for m in self.messages)
                token_ratio = current_tok / max(1, token_window)
            except Exception:
                _est = None

            msg_count_ratio = len(self.messages) / max(1, limit)
            effective_ratio = max(msg_count_ratio, token_ratio)

            # Compress when EITHER threshold is crossed: the configured
            # compress_ratio (message-count or token ratio, whichever is higher)
            # OR token pressure exceeding tok_trigger. Both thresholds are now
            # configurable; previously tok_trigger was a hardcoded 0.55 that
            # silently overrode compress_ratio for token pressure.
            if effective_ratio < ratio and token_ratio < tok_trigger:
                return

            # ── Snapshot before any await ──
            # Messages appended during the LLM call (by add_message, which is
            # sync and not blocked by this lock) are preserved at the tail.
            snapshot = list(self.messages)
            total_len = len(snapshot)

            # ── 1. Protected Head ──
            # Protect: index 0, first/all user messages, AND any previously
            # injected summary block (sender=系统, content starts with the
            # compression header) so multi-pass compression doesn't lose it.
            protected_head_indices = self._find_head_indices(
                snapshot, keep_all_users=keep_user_messages()
            )
            for i, m in enumerate(snapshot):
                if m.get("sender") == "系统" and is_compact_summary(m):
                    protected_head_indices.add(i)

            # ── 2. Protected Tail ──
            keep_recent = compression_keep_recent()
            protected_tail_indices = set(
                range(max(0, total_len - keep_recent), total_len)
            )

            # ── 3. Compressible Middle ──
            all_protected = protected_head_indices | protected_tail_indices

            head = [snapshot[i] for i in sorted(protected_head_indices)]
            tail = [
                snapshot[i]
                for i in sorted(protected_tail_indices)
                if i not in protected_head_indices
            ]
            to_compress = [
                snapshot[i] for i in range(total_len) if i not in all_protected
            ]

            if not to_compress:
                return

            # ── 3.5 Age tool logs in middle region before summarisation ──
            # Strip verbose previews from tool call logs to reduce summary input
            # size and preserve more semantic content in the compressed output.
            # 
            # IMPORTANT: we build a NEW message list rather than mutating originals.
            # Mutating `msg["content"]` in-place would corrupt self.messages even if
            # AI summarisation later fails and we fall back to `self.messages = head + tail`,
            # because `to_compress` holds direct references into self.messages.
            aged_to_compress: list[dict[str, str]] = []
            for msg in to_compress:
                original = msg["content"]
                aged = age_tool_log(original)
                # Preserve the original message reference for tail reconstruction
                if aged != original:
                    aged_to_compress.append({**msg, "content": aged})
                else:
                    aged_to_compress.append(msg)
            to_compress = aged_to_compress

            # ── 4a. AI Summarise ──
            if history_summarize_enabled() and self._provider is not None:
                history_text = "\n".join(
                    f"[{m['sender']}]: {m['content']}" for m in to_compress
                )
                prompt = (
                    f"以下是群聊的一段中期历史记录（共 {len(to_compress)} 条）。\n"
                    "请用简洁的中文摘要这些内容，重点保留核心发现、关键决策、重要事实以及已经完成的进度。\n"
                    "如果有具体的数值、文件路径或关键结论，请务必保留。\n"
                    "摘要须精炼，长度由调用方 max_tokens 控制。\n\n"
                    f"{history_text}"
                )
                max_tok = compress_max_summary_tokens()
                try:
                    # Flag add_message to append-only during the await: its
                    # trimming would replace self.messages with a new list,
                    # breaking the `self.messages[total_len:]` re-attach below.
                    self._compress_active = True
                    try:
                        response = await self._provider.chat_with_retry(
                            messages=[{"role": "user", "content": prompt}],
                            model=summarize_model(),
                            max_tokens=max_tok,
                        )
                    finally:
                        self._compress_active = False
                    summary = (response.content or "").strip()
                    if summary:
                        summary_msg = {
                            "sender": "系统",
                            "content": (
                                f"{_SUMMARY_PREFIX}（压缩了 {len(to_compress)} 条中间消息）]\n"
                                + summary
                            ),
                            # Structured boundary marker so subsequent passes
                            # protect this block without string-prefix
                            # sniffing. is_compact_summary() falls back to the
                            # prefix for legacy persisted messages.
                            "is_compact_summary": True,
                        }
                        # Rebuild from snapshot (NOT self.messages, which may
                        # have grown during the await). Insert summary at the
                        # position of the first compressible message, keeping
                        # all protected messages in chronological order.
                        summary_inserted = False
                        rebuilt: list[dict[str, str]] = []
                        for i, m in enumerate(snapshot):
                            if i in all_protected:
                                rebuilt.append(m)
                            elif not summary_inserted:
                                rebuilt.append(summary_msg)
                                summary_inserted = True
                        # Race-safety: append any messages added by add_message
                        # during the LLM call (they're not in `all_protected`
                        # and would otherwise be silently dropped).
                        appended_during = self.messages[total_len:]
                        rebuilt.extend(appended_during)
                        self.messages = rebuilt
                        logger.info(
                            "HistoryContext: AI compressed {} middle → summary "
                            "(head: {}, tail: {}, +{} appended)",
                            len(to_compress),
                            len(head),
                            len(tail),
                            len(appended_during),
                        )
                        return
                except Exception as e:
                    logger.warning(
                        "HistoryContext: AI compress failed: {}, falling back to mechanical compress", e
                    )

            # ── 4b. Fallback: mechanically compress middle region ──
            # Don't silently drop — build_compress_message keeps a short head
            # preview of each dropped message (capped at _LINE_CAP chars) so the
            # gist survives without an LLM call. Falls back to drop only if the
            # compress builder returns nothing (e.g. all-empty middle).
            compress_msg = build_compress_message(
                to_compress,
                max_chars=compress_fallback_chars(),
                sender_format=True,
            )
            rebuilt: list[dict[str, str]] = []
            compress_placed = False
            for i, m in enumerate(snapshot):
                if i in all_protected:
                    rebuilt.append(m)
                elif compress_msg is not None and not compress_placed:
                    rebuilt.append(compress_msg)
                    compress_placed = True
            if not compress_placed and compress_msg is not None:
                rebuilt.append(compress_msg)
            # Race-safety: append messages added during any await above.
            rebuilt.extend(self.messages[total_len:])
            self.messages = rebuilt
            logger.info(
                "HistoryContext: mechanically compressed {} middle messages "
                "(head: {}, tail: {})",
                len(to_compress),
                len(head),
                len(tail),
            )
