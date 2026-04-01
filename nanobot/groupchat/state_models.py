"""state_models.py — state.yaml 的 Pydantic schema。

纯变量驱动架构：
    - 控制变量（leader 修改）：state, reply_to, context_exclude, muted
    - 状态变量（系统更新，leader 只读）：activity, current_tool, cycle, ...

⚠️ 这些 model 定义了 state.yaml 的完整 schema。
    字段名不可随意改——leader prompt 和 poller 都依赖这些名字。
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict


class InboxMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_agent: str = Field(alias="from")
    content: str
    ts: str


class OutboxMessage(BaseModel):
    to: list[str]
    content: str
    ts: str


class ToolChainEntry(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    started: str
    finished: str | None = None
    ok: bool | None = None
    len: int | None = None
    preview: str | None = None


class AgentBlock(BaseModel):
    """每个 agent 在 state.yaml 中的 block。

    控制变量（leader 修改）：
        state           — running | paused（删掉 block = removed）
        reply_to        — All | "AgentName" | null
        context_exclude — 不让 agent 看到的 conversation seq 列表
        muted           — true = agent 运行但输出不显示

    状态变量（系统更新，leader 只读）：
        activity        — 当前活动：idle | thinking | tool_calling | replying
        current_tool    — 当前在调什么工具（null = 没在调）
        cycle           — 第几轮 tool_loop
        content_preview — 最新输出预览
        toolchain       — 工具调用链
        inbox / outbox  — 消息收发记录
    """
    # ── 控制变量 ──
    state: Literal["running", "paused"] = "running"
    reply_to: str | None = "All"
    context_exclude: list[int] = Field(default_factory=list)
    muted: bool = False

    # ── 状态变量 ──
    activity: Literal["idle", "thinking", "tool_calling", "replying"] = "idle"
    current_tool: str | None = None
    cycle: int = 0
    content_preview: str = ""
    toolchain: list[ToolChainEntry] = Field(default_factory=list)
    inbox: list[InboxMessage] = Field(default_factory=list)
    outbox: list[OutboxMessage] = Field(default_factory=list)


class SessionMeta(BaseModel):
    """会话元数据 — 精简到核心字段。
    
    status: leader 可设为 done 来结束群聊轮次。
    """
    id: str
    leader: str | None = None
    topic: str = ""
    round: int = 0
    status: Literal["running", "done"] = "running"


class ConversationEntry(BaseModel):
    seq: int
    sender: str
    content: str
    ts: str


class GroupChatStateData(BaseModel):
    """state.yaml 的顶层 schema。"""
    session: SessionMeta | None = None
    agents: dict[str, AgentBlock] = Field(default_factory=dict)
    conversation: list[ConversationEntry] = Field(default_factory=list)
    leader_data: dict[str, Any] = Field(default_factory=dict)
