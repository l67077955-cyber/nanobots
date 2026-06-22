"""Tests for nanobot.tools.summarizer."""

from unittest.mock import AsyncMock, patch

import pytest

summarizer_module = pytest.importorskip(
    "nanobot.tools.summarizer",
    reason="legacy summarizer module is not present in this checkout",
)
summarize_tool_output = summarizer_module.summarize_tool_output
_head_tail_truncate = summarizer_module._head_tail_truncate


def test_head_tail_truncate_short() -> None:
    """Short text is returned unchanged."""
    assert _head_tail_truncate("hello", 100) == "hello"


def test_head_tail_truncate_long() -> None:
    """Long text is head+tail truncated with a marker."""
    text = "A" * 200
    result = _head_tail_truncate(text, 100)
    assert "truncated" in result
    assert result.startswith("A" * 50)
    assert result.endswith("A" * 50)
    assert len(result) < len(text) + 100  # marker overhead


@pytest.mark.asyncio
async def test_short_output_passthrough() -> None:
    """Output below threshold is returned unchanged."""
    result, was_summarized = await summarize_tool_output("exec", "ok", threshold=100)
    assert result == "ok"
    assert was_summarized is False


@pytest.mark.asyncio
async def test_long_output_summarized() -> None:
    """Output above threshold triggers LLM summarization."""
    long_output = "x" * 10_000

    mock_usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    with patch(
        "nanobot.tools.summarizer._call_reader",
        new_callable=AsyncMock,
        return_value=("Summary: lots of x's", mock_usage),
    ):
        result, was_summarized = await summarize_tool_output(
            "exec", long_output, threshold=500,
        )

    assert was_summarized is True
    assert "Summary:" in result
    assert "[nano:exec]" in result
    assert len(result) < len(long_output)


@pytest.mark.asyncio
async def test_summarize_failure_fallback() -> None:
    """When LLM call fails, falls back to head+tail truncation."""
    long_output = "y" * 10_000

    with patch("nanobot.tools.summarizer._call_reader", side_effect=Exception("API down")):
        result, was_summarized = await summarize_tool_output(
            "exec", long_output, threshold=500,
        )

    assert was_summarized is False
    assert "truncated" in result
    assert len(result) < len(long_output) + 100


@pytest.mark.asyncio
async def test_summary_longer_than_original_falls_back() -> None:
    """If the summary is longer than original, falls back to truncation."""
    long_output = "z" * 1000

    mock_usage = {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}

    with patch(
        "nanobot.tools.summarizer._call_reader",
        new_callable=AsyncMock,
        return_value=("z" * 2000, mock_usage),
    ):
        result, was_summarized = await summarize_tool_output(
            "exec", long_output, threshold=500,
        )

    # Should fall back since summary was longer
    assert was_summarized is False
    assert "truncated" in result
