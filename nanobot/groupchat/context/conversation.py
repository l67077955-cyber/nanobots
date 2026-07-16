"""History-facing façade for collaboration code.

**History is the only context logic layer.** This module does not
process context itself — it routes collaboration call-sites to
``nanobot.core.history.History`` and optional I/O hooks.

- Writes → ``History.commit_turn`` / ``add_from_sender``
- Reads / LLM projection → ``view_for`` via PromptBuilder callback
  (builder must read History; it must not own a parallel store)
- display must not import this module for mutations
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from nanobot.core.history import History


@runtime_checkable
class ConversationPort(Protocol):
    """Narrow port: collaboration talks to History through this."""

    @property
    def history(self) -> History: ...

    def commit(self, sender: str, content: str) -> str:
        """Durable context write (History only)."""
        ...

    def latest_user_content(self, max_len: int = 300) -> str: ...

    def view_for(
        self,
        agent: str,
        *,
        build_prompt: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """LLM message projection from History-backed builder."""
        ...


@dataclass
class HistoryConversation:
    """Adapter around a History instance + optional persist hook.

    Parameters
    ----------
    history:
        The sole context store.
    on_write:
        Optional I/O callback ``(sender, content)`` after a successful
        History write (session log / disk). Not context logic.
    """

    history: History
    on_write: Callable[[str, str], None] | None = None

    def commit(self, sender: str, content: str) -> str:
        committed = self.history.commit_turn(sender, content)
        if committed and self.on_write is not None:
            self.on_write(sender, committed)
        return committed

    # Back-compat alias used by earlier ConversationContext name
    def add_from_sender(self, sender: str, content: str, **meta: Any) -> str:
        _ = meta
        return self.commit(sender, content)

    def latest_user_content(self, max_len: int = 300) -> str:
        return self.history.latest_user_content(max_len=max_len)

    def view_for(
        self,
        agent: str,
        *,
        build_prompt: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        _ = agent
        return build_prompt()


def conversation_from_engine(engine: Any) -> HistoryConversation:
    """Bind engine.history + engine persist hook."""

    def _on_write(sender: str, content: str) -> None:
        persist = getattr(engine, "_persist_after_history_write", None)
        if callable(persist):
            persist(sender, content)

    return HistoryConversation(history=engine.history, on_write=_on_write)


# Back-compat names
ConversationContext = ConversationPort
HistoryConversationContext = HistoryConversation


__all__ = [
    "ConversationPort",
    "HistoryConversation",
    "conversation_from_engine",
    "ConversationContext",
    "HistoryConversationContext",
]
