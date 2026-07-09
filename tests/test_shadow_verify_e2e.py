"""End-to-end shadow verification: real GroupChatEngine + FakeProvider.

The existing broadcast tests use _StubEngine (never hits real _build_agent_prompt),
and test_groupchat.py is an eval script needing a real API key. So the shadow
_shadow_verify_build (comparing ctx.build_for_groupchat vs history_to_messages)
was NEVER actually triggered in tests — "zero mismatch" was a false signal.

These tests instantiate a REAL GroupChatEngine with a FakeProvider (no network),
drive the real HistoryContext + ConversationContext + shadow History mirror, and
call _build_agent_prompt directly to trigger _shadow_verify_build. Any divergence
between ctx.build_for_groupchat and history_to_messages surfaces as a
"SHADOW MISMATCH" logger.error — captured by the shadow_mismatch fixture.

Coverage:
- basic: user + multi-agent messages → build
- rank visibility: agent msg with tool_log, asymmetric ranks → can_see_tool_call strip
- relevant_agents filter: a third agent excluded from the view
- after compress: overflow → maybe_compress (AI summary via FakeProvider) + shadow
  re-sync, then build
- multi-agent build: several agents build their views back-to-back
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from nanobot.groupchat.config import GroupChatAgentConfig, GroupChatConfig
from nanobot.groupchat.orchestra.engine import GroupChatEngine
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.tools.registry import ToolRegistry

# ── Fakes ───────────────────────────────────────────────────────────────

class FakeProvider(LLMProvider):
    """Returns a fixed response; records calls so compress' AI summary is deterministic."""

    def __init__(self, response: str = "好的，这是回复。") -> None:
        super().__init__(api_key="fake")
        self._response = response
        self.calls: int = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, metadata=None,
                   tool_choice=None, sampling_params=None) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self._response, finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake-model"


def _make_engine(tmp_path, monkeypatch, agent_names: list[str]) -> GroupChatEngine:
    """Real engine, isolated under tmp_path. _build_tool_registry is stubbed
    (returns an empty ToolRegistry) because _build_agent_prompt doesn't touch
    tools — we only want to exercise the real history + prompt build path."""
    agents = {
        # persona with a newline → _resolve_persona treats it as inline text,
        # not a file path (short single-line persona is read as a path → not found).
        name: GroupChatAgentConfig(model="fake-model", persona=f"你是 {name}，一位助手。\n")
        for name in agent_names
    }
    config = GroupChatConfig(agents=agents, max_history=50)
    monkeypatch.setattr(
        GroupChatEngine, "_build_tool_registry",
        lambda self, ws: ToolRegistry(),
    )
    # Isolate persistence: GroupChatState hardcodes ~/.nanobot as its state dir
    # and would otherwise restore the real chat_history.json into the test engine.
    import nanobot.groupchat.history.persistence as persistence
    monkeypatch.setattr(persistence, "_NANOBOT_DIR", tmp_path)
    engine = GroupChatEngine(config, FakeProvider(), tmp_path)
    engine._active_agents = list(agent_names)
    engine._leader = agent_names[0]
    return engine


# ── Loguru capture fixture ──────────────────────────────────────────────

@pytest.fixture
def shadow_mismatch():
    """Capture SHADOW MISMATCH ERROR logs. Empty list == zero divergence."""
    msgs: list[str] = []
    def _sink(message) -> None:
        text = str(message)
        if "SHADOW MISMATCH" in text:
            msgs.append(text)
    handle = logger.add(_sink, level="ERROR")
    try:
        yield msgs
    finally:
        logger.remove(handle)


# ── Tests ───────────────────────────────────────────────────────────────

TOOL_LOG = (
    "搜索完成。\n"
    "[工具调用记录]\n"
    "• web_search(query=\"python async\") → Python 异步编程指南... (123字)\n"
    "• web_fetch(url=\"...\") → 内容... (456字)"
)


def test_shadow_basic_no_mismatch(tmp_path, monkeypatch, shadow_mismatch):
    """Plain history (user + 2 agents) builds without divergence."""
    eng = _make_engine(tmp_path, monkeypatch, ["Harper", "Kirk"])
    eng._add_message("用户", "帮我搜索 Python 异步编程")
    eng._add_message("Harper", "好的，开始搜索")
    eng._add_message("Kirk", "我也来帮忙")
    eng._add_message("Harper", "找到了三篇文章")

    result = eng._build_agent_prompt("Kirk", relevant_agents=["Harper", "Kirk"])

    assert result, "build should produce messages"
    assert not shadow_mismatch, f"SHADOW MISMATCH:\n{''.join(shadow_mismatch)}"


def test_shadow_rank_visibility(tmp_path, monkeypatch, shadow_mismatch):
    """agent_ranks + tool_log: lower-rank viewer must not see higher-rank tool logs.
    ctx.build_for_groupchat and history_to_messages must agree on the strip."""
    eng = _make_engine(tmp_path, monkeypatch, ["Harper", "Kirk"])
    eng._add_message("用户", "搜索")
    eng._add_message("Harper", TOOL_LOG)   # Harper rank 1 (lower)
    eng._add_message("Kirk", "收到，补充一下")  # Kirk rank 2 (higher)
    ranks = {"Harper": 1, "Kirk": 2}

    # Kirk (rank 2) builds — sees Harper (rank 1) tool logs? visibility rule applies.
    eng._build_agent_prompt("Kirk", relevant_agents=["Harper", "Kirk"], agent_ranks=ranks)
    # Harper (rank 1) builds — sees Kirk (rank 2) tool logs? asymmetric.
    eng._build_agent_prompt("Harper", relevant_agents=["Harper", "Kirk"], agent_ranks=ranks)

    assert not shadow_mismatch, f"SHADOW MISMATCH (rank visibility):\n{''.join(shadow_mismatch)}"


def test_shadow_relevant_agents_filter(tmp_path, monkeypatch, shadow_mismatch):
    """relevant_agents whitelist excludes Ben from Kirk's view. ctx and truth
    must agree on the filter (no leaked agent message)."""
    eng = _make_engine(tmp_path, monkeypatch, ["Harper", "Kirk", "Ben"])
    eng._add_message("用户", "hi")
    eng._add_message("Harper", "a")
    eng._add_message("Kirk", "b")
    eng._add_message("Ben", "c (should be filtered out)")

    result = eng._build_agent_prompt("Kirk", relevant_agents=["Harper", "Kirk"])

    # Ben's message must not appear in Kirk's built prompt
    assert not any("should be filtered out" in (m.get("content") or "") for m in result)
    assert not shadow_mismatch, f"SHADOW MISMATCH (relevant_agents):\n{''.join(shadow_mismatch)}"


async def test_shadow_after_compress(tmp_path, monkeypatch, shadow_mismatch):
    """Overflow history → maybe_compress runs (FakeProvider returns a summary) →
    shadow re-syncs via from_sender_dicts → subsequent build must still match."""
    eng = _make_engine(tmp_path, monkeypatch, ["Harper", "Kirk"])
    eng._add_message("用户", "开始一个长对话")
    # 60 long messages → exceeds max_history=50 + token pressure → triggers compress
    for i in range(60):
        eng._add_message("Harper", f"第 {i} 条：这是一段足够长的内容用于施压 token 预算。" * 8)

    await eng._maybe_compress_history()  # microcompact + maybe_compress + shadow re-sync

    eng._build_agent_prompt("Kirk", relevant_agents=["Harper", "Kirk"])

    assert not shadow_mismatch, f"SHADOW MISMATCH (post-compress):\n{''.join(shadow_mismatch)}"


def test_shadow_multi_agent_build(tmp_path, monkeypatch, shadow_mismatch):
    """Each agent builds its own view back-to-back; all must match truth."""
    eng = _make_engine(tmp_path, monkeypatch, ["Harper", "Kirk", "Ben"])
    eng._add_message("用户", "大家讨论一下")
    eng._add_message("Harper", TOOL_LOG)
    eng._add_message("Kirk", "我的看法是…")
    eng._add_message("Ben", "补充…")
    ranks = {"Harper": 1, "Kirk": 2, "Ben": 3}

    for agent in ["Harper", "Kirk", "Ben"]:
        eng._build_agent_prompt(agent, relevant_agents=["Harper", "Kirk", "Ben"], agent_ranks=ranks)

    assert not shadow_mismatch, f"SHADOW MISMATCH (multi-build):\n{''.join(shadow_mismatch)}"


async def test_shadow_concurrent_build(tmp_path, monkeypatch, shadow_mismatch):
    """Multiple agents build concurrently in threads — read-only on _ctx/_history,
    must not observe a torn state that diverges from truth."""
    eng = _make_engine(tmp_path, monkeypatch, ["Harper", "Kirk", "Ben"])
    eng._add_message("用户", "并发构建测试")
    eng._add_message("Harper", "a")
    eng._add_message("Kirk", "b")
    eng._add_message("Ben", "c")

    await asyncio.gather(
        asyncio.to_thread(eng._build_agent_prompt, "Harper", ["Harper", "Kirk", "Ben"]),
        asyncio.to_thread(eng._build_agent_prompt, "Kirk", ["Harper", "Kirk", "Ben"]),
        asyncio.to_thread(eng._build_agent_prompt, "Ben", ["Harper", "Kirk", "Ben"]),
    )

    assert not shadow_mismatch, f"SHADOW MISMATCH (concurrent):\n{''.join(shadow_mismatch)}"
