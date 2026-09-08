"""Reusable memory-recall metric for history-compression experiments.

plan.md 批次 C2 第 2 条 ("做 nanobot 自己的 92% vs 58%"): the industry case
study saw production memory recall fall 92% → 58% after switching to
summarised compression while blind quality reviews stayed flat — silent
degradation.  This module makes that number measurable here, and
deterministically so:

  * :class:`Fact` — a machine-checkable fact (a distinctive *needle*
    substring) that an experiment plants into a history view at chosen
    positions (concrete numbers, file paths, decisions).
  * :func:`measure_recall` — recall of a fact set over a message list.
    A fact counts as recalled when its needle is still retrievable
    **verbatim in a single message** — either the original message survived
    (protected head/tail, truncation cutoff) or a summary preserved it.
  * :class:`DeterministicSummaryProvider` — a fake LLM provider whose
    summary keeps exactly the first *keep* planted facts (verbatim) and
    drops the rest.  Recall loss is therefore a pure function of *keep*,
    assertable by exact equality instead of a threshold.

Deliberately stdlib-only (no pytest import) so C2.4's controlled
experiments and scripts/ can reuse the kit:

  * inside tests/  →  ``from compression_recall_metric import ...``
    (pytest's prepend import mode puts tests/ on sys.path; there is no
    ``tests/__init__.py`` by design)
  * from repo root →  ``from tests.compression_recall_metric import ...``
    (implicit namespace package)
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "Fact",
    "RecallReport",
    "measure_recall",
    "DeterministicSummaryProvider",
    "extract_planted_facts",
]

# A planted fact message has content "FACT[<id>]: <statement>".  The history
# compressor renders the middle region as "[<sender>]: <content>" lines, so
# this pattern matches planted facts both in raw history text and inside the
# summarisation prompt.
_FACT_TAG_RE = re.compile(r"FACT\[([a-z0-9_-]+)\]: ([^\n]+)")


@dataclass(frozen=True)
class Fact:
    """One machine-checkable fact planted into history.

    *statement* is the sentence planted into the history; *needle* is the
    distinctive substring the metric searches for verbatim (pick something
    filler text can never contain: a specific number, a path, a name).
    """

    id: str
    statement: str
    needle: str

    def planted_content(self) -> str:
        """The message content to add_message() when planting this fact."""
        return f"FACT[{self.id}]: {self.statement}"


@dataclass(frozen=True)
class RecallReport:
    """Recall of a fact set over one message list, with per-fact detail."""

    total: int
    hits: tuple[str, ...]
    misses: tuple[str, ...]

    @property
    def recall(self) -> float:
        """hits / total; an empty fact set is vacuously full recall (1.0)."""
        if self.total == 0:
            return 1.0
        return len(self.hits) / self.total

    def detail(self) -> str:
        """One-line human-readable line for reports/logs."""
        return (
            f"recall={self.recall:.3f} ({len(self.hits)}/{self.total}) "
            f"hits={list(self.hits)} misses={list(self.misses)}"
        )


def measure_recall(
    facts: Sequence[Fact], messages: Iterable[Mapping]
) -> RecallReport:
    """Measure how many *facts* are still retrievable in *messages*.

    Args:
        facts: planted facts; ids must be unique (ValueError otherwise).
            ``hits``/``misses`` preserve this input order.
        messages: history-format message dicts (``{"sender", "content",
            "targets", ...}`` — exactly what ``HistoryContext.view_for``
            returns); entries without a ``content`` are treated as empty.

    Returns:
        A :class:`RecallReport` whose ``recall`` is the fraction of facts
        whose needle appears verbatim inside some single message.
    """
    seen: set[str] = set()
    for fact in facts:
        if fact.id in seen:
            raise ValueError(f"duplicate fact id: {fact.id!r}")
        seen.add(fact.id)

    contents = [str(m.get("content", "")) for m in messages]
    hits: list[str] = []
    misses: list[str] = []
    for fact in facts:
        if any(fact.needle in c for c in contents):
            hits.append(fact.id)
        else:
            misses.append(fact.id)
    return RecallReport(total=len(facts), hits=tuple(hits), misses=tuple(misses))


def extract_planted_facts(prompt_or_text: str) -> list[tuple[str, str]]:
    """Return ``(id, statement)`` for every planted FACT tag, in text order."""
    return _FACT_TAG_RE.findall(prompt_or_text)


class _SummaryResponse:
    """Minimal stand-in for the provider response the compressor expects."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.finish_reason = "stop"
        self.usage = {"prompt_tokens": 111, "completion_tokens": 22}
        self.cost = 0.0


class DeterministicSummaryProvider:
    """Fake summarisation provider with controllable, deterministic fact loss.

    ``chat_with_retry`` inspects the summarisation prompt, finds every
    planted ``FACT[...]: ...`` tag, and produces a summary that reproduces
    the **first** *keep* of them verbatim (statement included, so every
    kept fact's needle survives) while silently dropping the rest.  The
    drop is a pure function of the prompt and *keep* — recall degradation
    becomes exactly assertable, never flaky.
    """

    def __init__(self, keep: int = 0) -> None:
        if keep < 0:
            raise ValueError("keep must be >= 0")
        self.keep = keep
        #: every call: {"prompt", "model", "max_tokens", "metadata"}
        self.calls: list[dict] = []
        #: every summary content returned, in call order
        self.summaries: list[str] = []

    async def chat_with_retry(
        self, messages, model=None, max_tokens=None, metadata=None, **kwargs
    ):
        prompt = messages[0]["content"] if messages else ""
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
                "metadata": metadata,
            }
        )
        kept = extract_planted_facts(prompt)[: self.keep]
        if kept:
            content = "[摘要] 保留以下关键事实:\n" + "\n".join(
                f"事实 FACT[{fact_id}]: {statement}" for fact_id, statement in kept
            )
        else:
            content = "[摘要] 本段没有保留任何具体事实。"
        self.summaries.append(content)
        return _SummaryResponse(content)
