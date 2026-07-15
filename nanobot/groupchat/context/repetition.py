"""Cross-turn repetition detection for group-chat history.

Complements the within-response degenerate-repetition guard in
``tool_loop._has_contiguous_repeat`` (which catches the same sentence
repeated ≥3 times *inside one LLM response*). This module catches the
cross-turn case: an agent whose new message is near-identical to its own
*previous* message — i.e. it has nothing new to add this turn.

The guard is **observational only**: it logs a WARNING so the cross-turn
repeat is visible in ``gateway.log`` (mirroring how a leader agent would
manually notice "X 重复了之前的回复，没有新信息"). It deliberately does
not mutate or stub the content — stubbing an agent's just-spoken message
out of history risks the agent re-explaining next turn (it sees its
previous turn was abbreviated), which would *increase* repetition rather
than curb it. Convergence is left to the leader/scheduler, as before.
"""

from __future__ import annotations

from difflib import SequenceMatcher

# Below this similarity ratio the check short-circuits (treat as "no prior
# message" rather than "0% similar") — avoids noisy warnings early in a
# conversation or when an agent speaks for the first time.
_MIN_MEANINGFUL_LEN = 40


def _normalize(text: str) -> str:
    """Collapse whitespace for a stable similarity signal.

    We do NOT lowercase or strip punctuation: those change semantics for
    CJK text and offer little signal gain. Whitespace normalisation alone
    makes the comparison robust to indentation/reformatting differences.
    """
    if not text:
        return ""
    return " ".join(text.split())


def cross_turn_similarity(new: str, prev: str) -> float:
    """Similarity ratio in [0, 1] between an agent's new and previous message.

    Uses :class:`difflib.SequenceMatcher` on whitespace-normalised text —
    cheap (one call per add_message), adequate for catching near-duplicate
    turns. Returns 0.0 when either side is too short to be meaningful.
    """
    a = _normalize(new)
    b = _normalize(prev)
    if len(a) < _MIN_MEANINGFUL_LEN or len(b) < _MIN_MEANINGFUL_LEN:
        return 0.0
    # quickratio is ~4× faster than ratio() and accurate enough for a
    # tripwire; we never need sub-percent precision here.
    return SequenceMatcher(None, a, b).quick_ratio()


def is_cross_turn_repeat(
    new: str, prev: str, threshold: float = 0.85
) -> tuple[bool, float]:
    """Return ``(repeated, score)`` for an agent's new vs previous message.

    ``repeated`` is True only when ``score >= threshold`` AND both messages
    are long enough to be meaningful (so a one-line "ok" / "done" reply
    never trips the guard even if repeated verbatim).
    """
    score = cross_turn_similarity(new, prev)
    return (score >= threshold, score)
