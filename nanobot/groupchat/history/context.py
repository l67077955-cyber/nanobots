"""HistoryContext — single source of truth for conversation history.

Owns the message list and all related operations:
  - add_message     : append + enforce message-count & char-budget limits
  - maybe_compress  : AI summarize (or drop) the middle region on overflow
  - clear / format  : utility helpers

Engine and Broadcast delegate to this class instead of maintaining
``self._history`` directly.  Persistence is still delegated to
``GroupChatState.save_message()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.groupchat.history.message_converter import age_tool_log

if TYPE_CHECKING:
    from nanobot.groupchat.history.persistence import GroupChatState


class HistoryContext:
    """Encapsulates the shared conversation history for a **group chat** session.

    Distinct from ``Session`` (direct-chat mode): HistoryContext uses
    sender-based messages (``sender``/``content``) and persists via
    ``GroupChatState``; Session uses role-based OpenAI-format messages and
    per-session JSONL files. They serve different modes — see plan.md 4.2.

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
        # Phase D: per-agent persistent views.  Each agent's view is stored
        # independently, so compression operates only on that view without
        # affecting others.  Populated lazily as messages are added or as
        # agents become active (set via _active_agents setter).  The log
        # (self.messages) is the append-only source; views are projections
        # that each agent compresses independently.
        self._views: dict[str, list[dict]] = {}
        # Active agents list (set by engine._maybe_compress_history).  Used
        # by compress_all to know which views to iterate over.
        self._active_agents: list[str] = []

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
        self._views.clear()

    # ── Contract methods for external code (Phase E) ────────────────────────

    def is_empty(self) -> bool:
        """Return True if history has no messages."""
        return not self.messages

    def last_sender(self) -> str | None:
        """Return the sender of the most recent message, or None if empty."""
        if not self.messages:
            return None
        return self.messages[-1].get("sender")

    def has_system_message(self) -> bool:
        """Return True if any message has sender == '系统'."""
        return any(m.get("sender") == "系统" for m in self.messages)

    def all_messages(self) -> list[dict]:
        """Return a copy of all messages (for safe iteration)."""
        return [dict(m) for m in self.messages]

    def char_count(self) -> int:
        """Return total character count of all message contents."""
        return sum(len(m.get("content", "")) for m in self.messages)

    def provider(self) -> Any:
        """Return the LLM provider (for summarization)."""
        return self._provider

    def set_active_agents(self, agent_names: list[str]) -> None:
        """Set agents whose persistent views participate in compression."""
        self._active_agents = list(agent_names)

    def clear_agent_view(self, agent_name: str, keep_last: int = 0) -> int:
        """Clear messages from an agent's view (Leader 清理 agent 上下文).

        Removes the agent's own messages from its stored view, optionally
        keeping the last N.  Returns number removed.  Used by ClearContextTool
        so Leader can reset an agent's context without affecting others.
        """
        if agent_name not in self._views:
            return 0
        view = self._views[agent_name]
        agent_msgs = [m for m in view if m.get("sender") == agent_name]
        total = len(agent_msgs)
        remove_count = max(0, total - keep_last)
        if remove_count == 0:
            return 0
        # Remove oldest remove_count messages from that agent
        removed = 0
        new_view = []
        agent_seen = 0
        for m in view:
            if m.get("sender") == agent_name:
                agent_seen += 1
                if agent_seen <= remove_count:
                    removed += 1
                    continue
            new_view.append(m)
        self._views[agent_name] = new_view
        return removed

    def format(self) -> str:
        """Format history as a single readable string."""
        return "\n\n".join(
            f"[{m['sender']}]: {m['content']}" for m in self.messages
        )

    def add_message(self, sender: str, content: str, targets: list[str] | None = None) -> None:
        """Append a message and enforce message-count / char-budget limits.

        Head-protection guarantees that the very first message and the first
        user message are never evicted during trimming.

        Args:
            sender: The message sender's name.
            content: The message text.
            targets: Recipient agents this message is visible to.  ``None``
                means 全员可见 (everyone) and is stored as ``["All"]`` — the
                default for user / system / broadcast messages.  This is the
                visibility primitive per-agent views (Phase B) project on;
                it is purely additive, so pre-existing call sites that omit
                ``targets`` keep the old "everyone sees everything" behaviour.
        """
        if targets is None:
            targets = ["All"]
        else:
            # Copy to avoid the caller's list aliasing into stored history.
            targets = list(targets)
        msg = {"sender": sender, "content": content, "targets": targets}
        self.messages.append(msg)

        # Phase D: append to per-agent persistent views.  A message is added to
        # a view when: (1) "All" in targets, (2) agent_name in targets, or
        # (3) the agent is the sender (agents see their own sends).  Active
        # agents list is set by engine; we lazily create views for agents as
        # they become active.
        recipients = set(targets)
        if "All" in recipients:
            recipients = set(self._active_agents) | {sender}
        for agent_name in self._active_agents:
            if "All" in targets or agent_name in targets or agent_name == sender:
                if agent_name not in self._views:
                    self._views[agent_name] = []
                self._views[agent_name].append(dict(msg))

        try:
            from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
                keep_user_messages,
                max_context_chars,
                max_messages,
            )
            limit = max_messages()
            char_budget = max_context_chars()
            _keep_users = keep_user_messages()
        except Exception:
            limit = 150
            char_budget = 0
            _keep_users = False

        # ── Pre-identify protected head before any trimming ──
        head_indices = self._find_head_indices(self.messages, keep_all_users=_keep_users)
        head_msgs = [self.messages[i] for i in sorted(head_indices)]

        # Step 1: message-count limit — keep most-recent N, always keep head
        if len(self.messages) > limit:
            tail = self.messages[-limit:]
            tail_ids = {id(m) for m in tail}
            extra_head = [m for m in head_msgs if id(m) not in tail_ids]
            self.messages = extra_head + tail

        # Step 2: char-budget trimming — head is counted but always kept
        if char_budget > 0:
            head_indices = self._find_head_indices(self.messages, keep_all_users=_keep_users)
            head_msgs = [self.messages[i] for i in sorted(head_indices)]
            head_chars = sum(len(m.get("content", "")) for m in head_msgs)
            available = max(0, char_budget - head_chars)

            tail: list[dict] = []
            head_id_set = {id(m) for m in head_msgs}
            for m in reversed(self.messages):
                if id(m) in head_id_set:
                    continue
                c = len(m.get("content", ""))
                if available - c < 0:
                    break
                tail.insert(0, m)
                available -= c

            # Rebuild: head first (preserving order), then tail
            seen: set[int] = set()
            rebuilt: list[dict] = []
            for m in head_msgs + tail:
                if id(m) not in seen:
                    rebuilt.append(m)
                    seen.add(id(m))
            self.messages = rebuilt

        self._state.save_message(sender, content, self.messages, targets=targets)

    def view_for(self, agent_name: str) -> list[dict]:
        """Return the subset of history visible to *agent_name*.

        Visibility rule (the privacy invariant from plan.md): a message is
        visible to an agent when the agent is in the message's ``targets``,
        the message targets ``All`` (全员可见 — the default for user / system /
        broadcast messages), or the agent is the sender (an agent always sees
        what it itself sent, so it remembers its own contributions).

        Returns a fresh list of *copied* dicts so external callers cannot
        mutate the log by editing the returned view.  This is a live
        projection over ``self.messages`` — re-calling after new messages are
        added returns a view that includes them.

        Phase D: returns the stored persistent view (self._views[agent_name])
        if it exists; falls back to computing a fresh projection (for agents
        not yet active / pre-migration).  Callers get a copy, so mutating the
        returned list does not corrupt the stored view.
        """
        if agent_name in self._views:
            return [dict(m) for m in self._views[agent_name]]
        return self.view_for_raw(agent_name)

    def view_for_raw(self, agent_name: str) -> list[dict]:
        """Return this agent's uncompressed, visibility-filtered log view.

        Unlike :meth:`view_for`, this deliberately projects from the append-only
        log rather than a persistent per-agent view.  It is for diagnostic and
        quoting paths that need original text after a normal prompt view has
        been compressed.  As with every HistoryContext read method, callers
        receive copies and cannot mutate internal history.
        """
        visible: list[dict] = []
        for m in self.messages:
            tgts = m.get("targets") or ["All"]
            if "All" in tgts or agent_name in tgts or m.get("sender") == agent_name:
                visible.append(dict(m))
        return visible

    async def compress_for(self, agent_name: str) -> None:
        """Compress *agent_name*'s persistent view in place.

        Runs the same head/tail/summarise algorithm as ``maybe_compress`` but
        scoped to ``self._views[agent_name]``.  Compression of one view never
        touches another view — the privacy invariant holds: an A→B segment
        compressed in A's view stays invisible to C, because C's view never
        contained the segment.  Idempotent: a second call on an already-
        compressed view is a no-op (the summary is already in place).
        """
        if agent_name not in self._views:
            # Lazily materialize the view from the log on first compress
            self._views[agent_name] = self.view_for(agent_name)
        await self._compress_view(self._views[agent_name])

    async def compress_all(self) -> None:
        """Compress every active agent's view independently (plan.md Phase D).

        Replaces the old single ``maybe_compress`` over the shared list: each
        agent reaches its own threshold and compresses its own view.  Active
        agents are set by ``engine._maybe_compress_history``.
        """
        for name in list(self._active_agents):
            await self.compress_for(name)

    async def _compress_view(self, view: list[dict]) -> None:
        """The head/tail/summarise algorithm, operating on an arbitrary list.

        Extracted from ``maybe_compress`` so per-agent views and the shared
        log can both use it.  Mutates *view* in place.  When summarisation is
        disabled (or the provider is None), the middle region is KEPT (early
        return) rather than dropped — the old ``self.messages = head + tail``
        fallback at context.py:333-334 silently discarded history; per-agent
        compression must not lose data.
        """
        from nanobot.groupchat.history.history_settings import (  # noqa: PLC0415
            compress_max_summary_tokens,
            compress_ratio,
            compression_keep_recent,
            history_summarize_enabled,
            keep_user_messages,
            max_messages,
            summarize_model,
        )

        limit = max_messages()
        ratio = compress_ratio()
        if len(view) < int(limit * ratio):
            return

        total_len = len(view)

        protected_head_indices = self._find_head_indices(view, keep_all_users=keep_user_messages())
        keep_recent = compression_keep_recent()
        protected_tail_indices = set(range(max(0, total_len - keep_recent), total_len))
        all_protected = protected_head_indices | protected_tail_indices

        head = [view[i] for i in sorted(protected_head_indices)]
        tail = [view[i] for i in sorted(protected_tail_indices) if i not in protected_head_indices]
        to_compress = [view[i] for i in range(total_len) if i not in all_protected]
        if not to_compress:
            return

        # Age tool logs (build new dicts, never mutate originals)
        aged = []
        for msg in to_compress:
            original = msg["content"]
            if age_tool_log(original) != original:
                aged.append({**msg, "content": age_tool_log(original)})
            else:
                aged.append(msg)
        to_compress = aged

        if history_summarize_enabled() and self._provider is not None:
            history_text = "\n".join(f"[{m['sender']}]: {m['content']}" for m in to_compress)
            prompt = (
                f"以下是群聊的一段中期历史记录（共 {len(to_compress)} 条）。\n"
                "请用简洁的中文摘要这些内容，重点保留核心发现、关键决策、重要事实以及已经完成的进度。\n"
                "如果有具体的数值、文件路径或关键结论，请务必保留。\n"
                f"摘要不超过 500 字。\n\n{history_text}"
            )
            summary = ""
            for attempt in (1, 2):
                try:
                    response = await self._provider.chat_with_retry(
                        messages=[{"role": "user", "content": prompt}],
                        model=summarize_model(),
                        max_tokens=compress_max_summary_tokens(),
                    )
                except Exception as e:
                    logger.warning("HistoryContext: compress attempt {} failed: {}", attempt, e)
                    continue
                summary = (response.content or "").strip()
                if summary:
                    break
            if not summary:
                logger.warning("HistoryContext: keeping {} middle msgs uncompressed", len(to_compress))
                return

            summary_msg = {
                "sender": "系统",
                "content": f"[早期对话摘要（压缩了 {len(to_compress)} 条中间消息）]\n{summary}",
                "targets": ["All"],
            }
            rebuilt = []
            inserted = False
            for i, m in enumerate(view):
                if i in all_protected:
                    rebuilt.append(m)
                elif not inserted:
                    rebuilt.append(summary_msg)
                    inserted = True
            view[:] = rebuilt
            logger.info("HistoryContext: compressed {} → summary", len(to_compress))
            return

        # Summarisation disabled: KEEP the middle (do not discard)
        logger.info("HistoryContext: summarisation disabled, keeping {} middle msgs", len(to_compress))
        return
