"""Result store: archive oversized tool results, index stubs inline.

Pins the 2026-09-13 context-inflation fix (216K-char agent views): tool
results above the threshold become ``[已归档 tr-...]`` stubs in context;
originals are retrievable via the ``get_tool_result`` tool with paging.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.tools.result_store import (
    ResultStore,
    GetToolResultTool,
    compress_tool_results,
)


BIG = "x" * 8_000
SMALL = "ok"


@pytest.fixture()
def store(tmp_path: Path) -> ResultStore:
    return ResultStore(store_dir=tmp_path / "rs")


def test_put_get_roundtrip(store: ResultStore) -> None:
    idx = store.put(BIG)
    assert idx.startswith("tr-") and len(idx) == 13
    assert store.get(idx, limit=100_000) == BIG


def test_content_addressed_dedup(store: ResultStore) -> None:
    a = store.put("same content")
    b = store.put("same content")
    assert a == b
    assert store.size() == 1


def test_paging(store: ResultStore) -> None:
    idx = store.put("0123456789" * 100)  # 1000 chars
    page1 = store.get(idx, offset=0, limit=400)
    assert "0123456789" in page1 and "offset=400" in page1
    page2 = store.get(idx, offset=990, limit=400)
    assert "0123456789" in page2  # last 10 chars
    assert "offset=" not in page2  # last page: no more-pages hint


def test_missing_index_returns_error(store: ResultStore) -> None:
    out = store.get("tr-doesnotexist")
    assert out.startswith("Error: index")


def test_persistence_roundtrip(tmp_path: Path) -> None:
    d = tmp_path / "rs2"
    s1 = ResultStore(store_dir=d)
    idx = s1.put("persist me")
    s2 = ResultStore(store_dir=d)
    assert s2.get(idx, limit=100) == "persist me"


def test_compress_replaces_old_large_results(store: ResultStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot.tools import result_store as rs_mod
    monkeypatch.setattr(rs_mod, "get_result_store", lambda: store)
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "web_fetch", "arguments": "{}"}},
            {"id": "t2", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": BIG},      # old + big → archived
        {"role": "tool", "tool_call_id": "t2", "content": BIG},      # newest 3 kept... see next msg
        {"role": "tool", "tool_call_id": "t3", "content": BIG},
        {"role": "tool", "tool_call_id": "t4", "content": BIG},      # recent, kept verbatim
        {"role": "tool", "tool_call_id": "t5", "content": SMALL},    # recent, kept verbatim
    ]
    out = compress_tool_results(msgs, keep_recent=3, threshold=6_000)
    # t1/t2 are outside keep_recent(3): t1 archived; t2 is index 3, keep_recent covers last 3 (t3? count: positions of tools = [2,3,4,5,6]; last 3 = [4,5,6] → archivable [2,3] → t1,t2 archived
    assert "[已归档 tr-" in out[2]["content"]
    assert "[已归档 tr-" in out[3]["content"]
    assert out[4]["content"] == BIG
    assert out[5]["content"] == BIG
    assert out[6]["content"] == SMALL
    # caller list untouched (copy-on-write)
    assert msgs[2]["content"] == BIG
    # archived content retrievable
    idx = out[2]["content"].split("tr-")[1].split()[0]
    assert store.get("tr-" + idx, limit=100_000) == BIG


def test_compress_idempotent_on_stubs() -> None:
    stub = "[已归档 tr-abc1234567 | 原文 8,000 字符 | 调用 get_tool_result(index=\"tr-abc1234567\")]"
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": stub},
        {"role": "tool", "tool_call_id": "t2", "content": "x"},
    ]
    out = compress_tool_results(msgs, keep_recent=0, threshold=10)
    assert out is msgs or out == msgs  # stub not re-processed


def test_small_results_untouched() -> None:
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": SMALL},
        {"role": "tool", "tool_call_id": "t2", "content": "also small"},
    ]
    out = compress_tool_results(msgs, keep_recent=0, threshold=6_000)
    assert out is msgs


async def test_get_tool_result_tool(store: ResultStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from nanobot.tools import result_store as rs_mod
    monkeypatch.setattr(rs_mod, "get_result_store", lambda: store)
    tool = GetToolResultTool()
    idx = store.put("A" * 10)
    out = await tool.execute(index=idx, limit=5)
    assert "AAAAA" in out and "offset=5" in out
    err = await tool.execute(index="")
    assert err.startswith("Error")
    missing = await tool.execute(index="tr-nope")
    assert missing.startswith("Error: index")


def test_history_context_archives_oversized_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Groupchat views must not accumulate >threshold text blobs."""
    from nanobot.groupchat.history.context import HistoryContext
    from nanobot.tools import result_store as rs_mod

    store = ResultStore(store_dir=tmp_path / "rs3")
    monkeypatch.setattr(rs_mod, "get_result_store", lambda: store)

    state = type("S", (), {"save_message": lambda *a, **k: None})()
    ctx = HistoryContext(state=state, provider=None)
    ctx.set_active_agents(["A"])
    big = "历史大块文本" + "Z" * 9_000
    ctx.add_message("Harper", big)
    stored = ctx.messages[-1]["content"]
    assert "[已归档 tr-" in stored and len(stored) < 400
    view = ctx.view_for("A")
    assert len(view[-1]["content"]) < 400
    # original retrievable
    idx = stored.split("tr-")[1].split()[0]
    assert store.get("tr-" + idx, limit=100_000) == big
    # small content passes through verbatim
    ctx.add_message("Harper", "小消息")
    assert ctx.messages[-1]["content"] == "小消息"
