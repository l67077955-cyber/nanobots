"""Async group chat engine for multi-agent discussions.

Supports fluid agent management:
- Agent registry: all available agents (loaded from config/directory)
- Active participants: agents currently in the conversation
- Seamlessly transitions between 1-on-1 and group chat as agents are added/removed
"""

from __future__ import annotations

import asyncio
import json
import random
import time as _time
from datetime import datetime, timezone, timedelta

_CST = timezone(timedelta(hours=8))

def _cn_now() -> datetime:
    """Return current time in China Standard Time (UTC+8)."""
    return datetime.now(_CST)
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.agents import load_agents
from nanobot.groupchat.config import GroupChatConfig
from nanobot.groupchat.mailbox import MailboxHub
from nanobot.providers.base import LLMProvider


class GroupChatEngine:
    """Async multi-agent group chat engine with fluid agent management.

    Usage:
        engine = GroupChatEngine(config, provider, workspace)

        # Registry is pre-loaded; active participants start empty
        engine.add_agent("Benjamin")    # 1 agent → direct replies
        engine.add_agent("Grok")        # 2+ agents → group chat auto-starts
        engine.remove_agent("Benjamin") # back to 1 → group loop stops
        engine.remove_agent("Grok")     # 0 agents → silent
    """

    # All available tool names for granular control
    TOOL_NAMES = [
        "web_search", "web_fetch", "exec",
        "read_file", "write_file", "edit_file", "list_dir",
        "chatroom_send", "wait",
    ]

    def __init__(
        self,
        config: GroupChatConfig,
        provider: LLMProvider,
        workspace: Path,
        brave_api_key: str = "",
    ):
        self.config = config
        self.provider = provider
        self.workspace = workspace
        self.brave_api_key = brave_api_key
        self._pm_cache: dict | None = None
        self._mailbox = MailboxHub()
        self._mode: str = self._load_mode()
        self._register_tools()

    def _register_tools(self) -> None:
        # Tool registry (same tools as core AgentLoop)
        # Lazy import to avoid circular: engine → agent.tools → agent → config → groupchat
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
        from nanobot.agent.tools.shell import ExecTool
        from nanobot.agent.tools.filesystem import (
            ReadFileTool, WriteFileTool, EditFileTool, ListDirTool,
        )

        home = str(Path.home())
        sandbox = Path("/tmp")

        # Web + file + exec tools (available to agents with tools_enabled)
        self.tools = ToolRegistry()
        self.tools.register(WebSearchTool(api_key=self.brave_api_key or None))
        self.tools.register(WebFetchTool())
        self.tools.register(ExecTool(timeout=120, working_dir=home))
        self.tools.register(ReadFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.tools.register(WriteFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.tools.register(EditFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.tools.register(ListDirTool(workspace=sandbox, allowed_dir=sandbox))

        # Register chatroom tools (agent name set per-call)
        from nanobot.groupchat.chatroom_tools import ChatroomSendTool, WaitTool
        self._chatroom_send_tool = ChatroomSendTool(mailbox=self._mailbox)
        self._wait_tool = WaitTool(mailbox=self._mailbox)
        self.tools.register(self._chatroom_send_tool)
        self.tools.register(self._wait_tool)

        # Direct chat tools (default agent gets same + full exec)
        self.direct_tools = ToolRegistry()
        self.direct_tools.register(ExecTool(timeout=120, working_dir=home))
        self.direct_tools.register(ReadFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(WriteFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(EditFileTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(ListDirTool(workspace=sandbox, allowed_dir=sandbox))
        self.direct_tools.register(WebSearchTool(api_key=self.brave_api_key or None))
        self.direct_tools.register(WebFetchTool())
        logger.info("Groupchat: registered {} group tools, {} direct tools",
                    len(self.tools), len(self.direct_tools))

        # Registry: all known agents {name: {model, prompt}}
        self.registry: dict[str, dict[str, Any]] = load_agents(self.config, self.workspace)

        # Active participants in current conversation
        self._active_agents: list[str] = self._load_active()

        # Leader mode
        self._leader: str | None = self._load_leader()
        self._round: int = 0  # Current round number

        # Runtime state
        self._task: asyncio.Task | None = None
        self._running = False
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._send_fn: Callable[[str], Awaitable[None]] | None = None
        self._edit_fn: Callable[[int, str], Awaitable[None]] | None = None
        self._on_round_done: Callable[[], Awaitable[None]] | None = None
        self._send_and_get_id_fn: Callable[[str], Awaitable[int | None]] | None = None
        self._topic: str = ""
        self._history: list[dict[str, str]] = []
        self._request_log: list[dict[str, Any]] = []  # LLM request log
        self._session_dir: Path | None = None
        self._debug_context: bool = False  # /debug toggles context breakdown logs
        self._prompt_order: dict[str, list[str]] = self._load_prompt_order()

    # ── Prompt component keys (in default order) ──
    _DEFAULT_PROMPT_ORDER = [
        "main_prompt",
        "group_context",
        "persona",
        "tool_instructions",
        "broadcast_hint",
        "examples",
        "history",
        "instructions",
        "leader_prompt",
        "group_nudge",
    ]
    _COMPONENT_LABELS = {
        "main_prompt": "主提示 (main_prompt)",
        "group_context": "群聊上下文 (group_context)",
        "persona": "人设/SOUL (persona)",
        "tool_instructions": "工具指令 (tool_instructions)",
        "broadcast_hint": "广播协调 (broadcast_hint)",
        "examples": "示例对话 (examples)",
        "history": "聊天记录 (history)",
        "instructions": "后置指令 (instructions)",
        "leader_prompt": "领袖指令 (leader_prompt)",
        "group_nudge": "群聊规范 (group_nudge)",
    }
    # Components editable via /prompt (global templates with {{agent}})
    _GLOBAL_EDITABLE = {
        "main_prompt", "group_context", "tool_instructions", "broadcast_hint",
        "examples", "instructions", "leader_prompt", "group_nudge",
    }
    # Components editable only via /editagent (per-agent files)
    _AGENT_EDITABLE = {"persona"}
    _EDITABLE_COMPONENTS = _GLOBAL_EDITABLE | _AGENT_EDITABLE

    def _load_prompt_order(self) -> dict[str, list[str]]:
        f = Path.home() / ".nanobot" / "prompt_order.json"
        if f.exists():
            try:
                data = json.loads(f.read_text())
                if isinstance(data, list):
                    return {"default": data}
                return data
            except Exception:
                pass
        return {}

    def _save_prompt_order(self) -> None:
        f = Path.home() / ".nanobot" / "prompt_order.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(self._prompt_order, ensure_ascii=False, indent=2))

    def get_agent_prompt_order(self, agent_name: str = "") -> list[str]:
        """Get prompt component order (same for all agents)."""
        return list(self._prompt_order.get("default", self._DEFAULT_PROMPT_ORDER))

    def set_default_prompt_order(self, order: list[str]) -> None:
        """Set prompt component order for all agents."""
        self._prompt_order["default"] = order
        self._save_prompt_order()

    def remove_prompt_component(self, idx: int) -> str:
        """Remove a component from the order by index."""
        order = self.get_agent_prompt_order()
        if idx < 0 or idx >= len(order):
            return "❌ 无效索引"
        key = order[idx]
        if key == "history":
            return "❌ 聊天记录 (history) 不可删除"
        label = self._COMPONENT_LABELS.get(key, key)
        order.pop(idx)
        self.set_default_prompt_order(order)
        return f"🗑 已移除: {label}"

    def get_available_components(self) -> list[str]:
        """Return components not in current order (can be added back)."""
        current = set(self.get_agent_prompt_order())
        return [k for k in self._DEFAULT_PROMPT_ORDER if k not in current]

    def get_prompt_components(self, agent_name: str) -> list[dict[str, Any]]:
        """Return component list with key, label, content preview, char count, editable flag."""
        agent = self.registry.get(agent_name, {})
        order = self.get_agent_prompt_order(agent_name)
        components = []
        for key in order:
            content = self._get_component_content(agent_name, agent, key)
            components.append({
                "key": key,
                "label": self._COMPONENT_LABELS.get(key, key),
                "content": content,
                "chars": len(content) if content else 0,
                "editable": key in self._EDITABLE_COMPONENTS,
            })
        return components

    def _get_component_content(self, agent_name: str, agent: dict, key: str) -> str:
        if key == "main_prompt":
            return (
                f"Write {agent_name}'s next reply in a fictional group chat. "
                f"Write 1 reply only in character as {agent_name}. "
                f"Do not write as or for other characters."
            )
        elif key == "group_context":
            members = ", ".join(self._active_agents) if self._active_agents else "(无)"
            now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
            return f"[Start a new group chat. Group members: {members}]\n[Current date and time: {now}]"
        elif key == "persona":
            return agent.get("prompt", "")
        elif key == "tool_instructions":
            return ""
        elif key == "examples":
            return agent.get("examples", "")
        elif key == "history":
            return f"[聊天记录 — {len(self._history)} 条消息]"
        elif key == "instructions":
            return agent.get("instructions", "")
        elif key == "leader_prompt":
            if self._leader == agent_name:
                return "[Leader prompt — 自动生成]"
            return ""
        elif key == "broadcast_hint":
            # Only rendered when broadcast mode is active;
            # placeholders filled by broadcast.py
            return ""
        elif key == "group_nudge":
            return (
                f"[Write the next reply only as {agent_name}. "
                f"Do NOT write dialogue for other characters. "
                f"Do NOT prefix your reply with your name (e.g. '{agent_name}:'). "
                f"Do NOT simulate tool calls in text (e.g. [Search ...], [Check ...]). "
                f"Stay in character and respond naturally.]"
            )
        return ""

    def _get_component_template(self, key: str) -> str:
        """Return the default template for a component, using {{agent}} as placeholder."""
        templates = {
            "main_prompt": (
                "Write {{agent}}'s next reply in a group chat. "
                "Write 1 reply only in character as {{agent}}. "
                "Do not write as or for other characters. "
                "Focus on executing the user's request — do not just greet or ask what to do."
            ),
            "group_context": (
                "[Start a new group chat. Group members: {{members}}]\n"
                "[Current date and time: {{datetime}}]"
            ),
            "persona": "[从 SOUL.md 加载 — 在 /editagent 中编辑]",
            "tool_instructions": (
                "[工具使用规范]\n\n"
                "可用工具: exec, read_file, write_file, edit_file, list_dir, "
                "web_search, web_fetch, chatroom_send, wait\n\n"
                "## 核心原则\n"
                "- 有工具就用，禁止说「我没有能力」「我无法搜索」。\n"
                "- 意图明确就直接执行，不要问「需要我搜索吗？」。\n"
                "- 每次工具调用后检查结果再决定下一步。\n"
                "- 工具失败→换方案，不要重复同一调用。\n\n"
                "## 搜索规范\n"
                "- 时事/新闻: web_search(query=..., freshness=\"pd\") 或 \"pw\"\n"
                "- 用户给的URL: web_fetch(url=...) 直接读取\n\n"
                "## 协作通信协议\n"
                "chatroom_send(to=\"Harper\", message=\"搜索结果...\")\n"
                "chatroom_send(to=\"All\", message=\"关键发现...\")\n\n"
                "### 收到消息后的响应规则（关键！）\n"
                "收到队友消息时：执行请求 → 用 chatroom_send 回复结果。\n"
                "禁止：只在最终回复里提到，而不通过 chatroom_send 回复发送者。\n\n"
                "wait(timeout=30) 等待消息，不要超过60s。"
            ),
            "broadcast_hint": (
                "[广播模式 — 技术研究协作环境]\n"
                "你是 {{agent_idx}}/{{total}} 号研究助手，代号 {{agent}}\n"
                "队友: {{teammates}}\n\n"
                "⚠️ 研究任务: {{user_question}}\n\n"
                "## 立即行动\n"
                "使用工具执行任务。不要打招呼、不要问「有什么任务」。\n\n"
                "## 协作流程\n"
                "1. 立即搜索，选择与队友不同的角度/关键词\n"
                "2. 搜到初步结果 → chatroom_send(to=\"All\", message=\"初步结果: ...\") 分享\n"
                "3. 继续深入 → chatroom_send 发送更新版本\n"
                "4. 收到队友结果 → chatroom_send 回复确认或补充\n"
                "5. wait 超时没收到回复 → 主动再发一次提醒\n\n"
                "## 通信时序\n"
                "发消息: chatroom_send(to=\"目标\", message=\"内容\")\n"
                "等回复: wait(timeout=30)\n"
                "超时继续工作并主动再分享"
            ),
            "examples": "",
            "history": "[聊天记录 — 自动插入]",
            "instructions": "",
            "leader_prompt": (
                "[你是 GROUP LEADER 👑。你的职责：\n"
                "- 分析用户请求，制定计划\n"
                "- 其他 agent 已先发言，请审阅他们的回复\n"
                "- 纠正错误信息，补充遗漏，去重整合\n"
                "- 给出最终汇总回复]"
            ),
            "group_nudge": (
                "[Write the next reply only as {{agent}}. "
                "Do NOT write dialogue for other characters. "
                "Do NOT prefix your reply with your name (e.g. '{{agent}}:'). "
                "Do NOT simulate tool calls in text — no XML tags like <web_search>, <tool>, "
                "<function_call>, [Search ...], [Check ...] etc. "
                "If you need to use a tool, use the function calling API, not text. "
                "Stay in character and respond naturally.]"
            ),
        }
        return templates.get(key, "")

    def update_prompt_component(self, agent_name: str, key: str, content: str) -> str:
        """Update a component's content. Persists to file where applicable."""
        if key not in self._EDITABLE_COMPONENTS:
            return f"❌ 组件 '{key}' 不可编辑"

        # Global template edit (from /prompt)
        if agent_name == "__global__":
            overrides_file = Path.home() / ".nanobot" / "prompt_overrides.json"
            overrides: dict = {}
            if overrides_file.exists():
                try:
                    overrides = json.loads(overrides_file.read_text())
                except Exception:
                    pass
            overrides.setdefault("__global__", {})[key] = content
            overrides_file.parent.mkdir(parents=True, exist_ok=True)
            overrides_file.write_text(json.dumps(overrides, ensure_ascii=False, indent=2))
            label = self._COMPONENT_LABELS.get(key, key)
            return f"✅ 已更新全局模板: {label}\n💡 使用 {{{{agent}}}} 代表 agent 名字"

        # Per-agent edit (from /editagent)
        agent = self.registry.get(agent_name)
        if not agent:
            return f"❌ Agent '{agent_name}' 不存在"

        if key == "persona":
            agent["prompt"] = content
            self._persist_agent_file(agent_name, "SOUL.md", content)
        elif key == "examples":
            agent["examples"] = content
            self._persist_agent_file(agent_name, "EXAMPLES.md", content)
        elif key == "instructions":
            agent["instructions"] = content
            self._persist_agent_file(agent_name, "INSTRUCTIONS.md", content)
        elif key in ("main_prompt", "tool_instructions", "group_nudge"):
            # These are templates — store overrides in a JSON file
            overrides_file = Path.home() / ".nanobot" / "prompt_overrides.json"
            overrides: dict = {}
            if overrides_file.exists():
                try:
                    overrides = json.loads(overrides_file.read_text())
                except Exception:
                    pass
            overrides.setdefault(agent_name, {})[key] = content
            overrides_file.write_text(json.dumps(overrides, ensure_ascii=False, indent=2))
            # Also update in-memory for _get_component_content
            agent[f"_override_{key}"] = content

        return f"✅ 已更新 {agent_name} 的 {self._COMPONENT_LABELS.get(key, key)}"

    def _persist_agent_file(self, agent_name: str, filename: str, content: str) -> None:
        """Write content to the agent's workspace file."""
        agents_dir = Path(self.config.agents_dir or "~/.nanobot/agents").expanduser()
        if not agents_dir.is_absolute():
            agents_dir = self.workspace / self.config.agents_dir
        # Find the agent dir (case-insensitive match)
        for d in agents_dir.iterdir():
            if d.is_dir() and d.name.lower() == agent_name.lower():
                ws = d / "workspace"
                ws.mkdir(parents=True, exist_ok=True)
                (ws / filename).write_text(content)
                logger.info("Persisted {} for agent {} ({} chars)", filename, agent_name, len(content))
                return
        logger.warning("Could not find agent dir for {}", agent_name)

    @property
    def is_running(self) -> bool:
        """True when group chat loop is actively running (2+ agents)."""
        return self._running

    @property
    def active_agents(self) -> list[str]:
        return list(self._active_agents)

    @property
    def available_agents(self) -> list[str]:
        return list(self.registry.keys())

    def set_send_fn(self, send_fn: Callable[[str], Awaitable[None]]) -> None:
        """Set the message output callback."""
        self._send_fn = send_fn

    def set_edit_fn(
        self,
        edit_fn: Callable[[int, str], Awaitable[None]],
        send_and_get_id_fn: Callable[[str], Awaitable[int | None]],
    ) -> None:
        """Set callbacks for streaming message edits."""
        self._edit_fn = edit_fn
        self._send_and_get_id_fn = send_and_get_id_fn

    def set_on_round_done(self, cb: Callable[[], Awaitable[None]]) -> None:
        """Set callback invoked when all agents finish speaking in a round."""
        self._on_round_done = cb

    # ── Agent Management ─────────────────────────────────────

    def add_agent(self, name: str) -> str:
        """Add an agent to the active conversation.

        Returns status message.
        """
        # Case-insensitive lookup
        matched = self._resolve_agent_name(name)
        if not matched:
            return f"❌ Agent '{name}' 不存在。可用: {', '.join(self.registry.keys())}"

        if matched in self._active_agents:
            return f"⚠️ {matched} 已在对话中"

        self._active_agents.append(matched)
        self._save_active()
        logger.info("Groupchat: added agent {}, active={}", matched, self._active_agents)

        # Don't auto-start loop here — inject() will lazy-start
        # when user sends the first message with 2+ agents.
        if len(self._active_agents) >= 2:
            return (
                f"✅ {matched} 加入对话！\n"
                f"👥 当前成员: {', '.join(self._active_agents)}\n"
                f"📌 直接发消息开始群聊"
            )

        return f"✅ {matched} 加入对话\n👥 当前成员: {', '.join(self._active_agents)}"

    def remove_agent(self, name: str) -> str:
        """Remove an agent from the active conversation.

        Returns status message.
        """
        matched = self._resolve_agent_name(name)
        if not matched or matched not in self._active_agents:
            return f"⚠️ {name} 不在当前对话中。当前: {', '.join(self._active_agents) or '无'}"

        self._active_agents.remove(matched)
        self._save_active()
        logger.info("Groupchat: removed agent {}, active={}", matched, self._active_agents)

        # If below 2 agents, stop group loop
        if len(self._active_agents) < 2 and self._running:
            self._stop_group_loop()
            if self._active_agents:
                return (
                    f"✅ {matched} 已离开\n"
                    f"💬 回到与 {self._active_agents[0]} 的对话模式"
                )
            return f"✅ {matched} 已离开，无活跃 agent"

        return f"✅ {matched} 已离开\n👥 当前成员: {', '.join(self._active_agents)}"
    # ── Agent ordering ────────────────────────────────────

    def reorder_agents(self, new_order: list[str]) -> str:
        """Reorder active agents. new_order should contain all active agent names."""
        # Validate all names exist in active list
        resolved = []
        for name in new_order:
            matched = self._resolve_agent_name(name)
            if not matched or matched not in self._active_agents:
                return f"⚠️ {name} 不在活跃列表中"
            if matched in resolved:
                return f"⚠️ {matched} 重复了"
            resolved.append(matched)

        if set(resolved) != set(self._active_agents):
            missing = set(self._active_agents) - set(resolved)
            return f"⚠️ 缺少: {', '.join(missing)}"

        self._active_agents[:] = resolved
        self._save_active()
        # Auto-update saved group if one is loaded
        if hasattr(self, '_current_group_name') and self._current_group_name:
            groups = self._load_groups()
            if self._current_group_name in groups:
                groups[self._current_group_name] = list(resolved)
                self._save_groups(groups)
        order_str = " → ".join(resolved)
        return f"✅ 发言顺序已调整\n📢 {order_str}"

    # ── Leader Mode ─────────────────────────────────────────

    @property
    def leader(self) -> str | None:
        return self._leader

    def set_leader(self, name: str | None) -> str:
        """Set or clear the leader agent."""
        if name is None:
            old = self._leader
            self._leader = None
            self._save_leader()
            return f"✅ 已取消 Leader 模式" + (f" ({old})" if old else "")

        matched = self._resolve_agent_name(name)
        if not matched:
            return f"❌ Agent '{name}' 不存在。可用: {', '.join(self.registry.keys())}"

        self._leader = matched
        self._save_leader()
        return f"👑 {matched} 已设为 Leader\n其他 agent 会先发言，{matched} 最后汇总"

    def _save_leader(self) -> None:
        try:
            p = Path.home() / ".nanobot" / "leader.txt"
            if self._leader:
                p.write_text(self._leader)
            elif p.exists():
                p.unlink()
        except Exception:
            pass

    def _load_leader(self) -> str | None:
        p = Path.home() / ".nanobot" / "leader.txt"
        if p.exists():
            try:
                name = p.read_text().strip()
                if name and name in self.registry:
                    logger.info("Restored leader: {}", name)
                    return name
            except Exception:
                pass
        return None

    # ── Mode Management (serial / broadcast) ────────────────

    def set_mode(self, mode: str) -> str:
        """Switch group chat execution mode."""
        mode = mode.lower().strip()
        if mode not in ("serial", "broadcast"):
            return f"❌ 未知模式 '{mode}'，可选: serial, broadcast"
        old = self._mode
        self._mode = mode
        self._save_mode()
        labels = {"serial": "串行轮流", "broadcast": "广播乱序"}
        return f"✅ 模式切换: {labels.get(old, old)} → {labels.get(mode, mode)}"

    def _save_mode(self) -> None:
        try:
            p = Path.home() / ".nanobot" / "chat_mode.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self._mode)
        except Exception:
            pass

    def _load_mode(self) -> str:
        p = Path.home() / ".nanobot" / "chat_mode.txt"
        if p.exists():
            try:
                mode = p.read_text().strip()
                if mode in ("serial", "broadcast"):
                    logger.info("Restored chat mode: {}", mode)
                    return mode
            except Exception:
                pass
        return self.config.mode or "serial"

    # ── Persistence ─────────────────────────────────────────

    @property
    def _active_file(self) -> Path:
        return Path.home() / ".nanobot" / "active_agents.json"

    def _save_active(self) -> None:
        """Persist active agents list (and order) to disk."""
        try:
            self._active_file.parent.mkdir(parents=True, exist_ok=True)
            self._active_file.write_text(json.dumps(self._active_agents, ensure_ascii=False))
        except Exception:
            pass

    def _load_active(self) -> list[str]:
        """Load active agents from disk, filtering out unregistered ones."""
        if self._active_file.exists():
            try:
                saved = json.loads(self._active_file.read_text())
                # Only keep agents that exist in registry
                valid = [a for a in saved if a in self.registry]
                if valid:
                    logger.info("Restored active agents: {}", valid)
                return valid
            except Exception:
                pass
        return []

    @property
    def _groups_file(self) -> Path:
        return Path.home() / ".nanobot" / "groups.json"

    def _load_groups(self) -> dict[str, list[str]]:
        if self._groups_file.exists():
            try:
                return json.loads(self._groups_file.read_text())
            except Exception:
                return {}
        return {}

    def _save_groups(self, groups: dict[str, list[str]]) -> None:
        self._groups_file.parent.mkdir(parents=True, exist_ok=True)
        self._groups_file.write_text(json.dumps(groups, ensure_ascii=False, indent=2))

    def save_group(self, name: str) -> str:
        """Save current active agents as a named group."""
        if not self._active_agents:
            return "⚠️ 没有活跃 agent，无法保存"
        groups = self._load_groups()
        groups[name] = list(self._active_agents)
        self._save_groups(groups)
        return f"✅ 已保存分组 「{name}」\n👥 成员: {', '.join(self._active_agents)}"

    def load_group(self, name: str) -> str:
        """Load a saved group config, setting agents directly."""
        groups = self._load_groups()
        if name not in groups:
            available = ', '.join(groups.keys()) if groups else '无'
            return f"⚠️ 分组 「{name}」 不存在\n📋 可用分组: {available}"

        target = groups[name]

        # Stop any running loop first
        self._stop_group_loop()

        # Set agents directly (no add/remove to avoid loop race)
        valid = [a for a in target if self._resolve_agent_name(a)]
        self._active_agents = valid
        self._save_active()

        # Start group loop if 2+ agents
        if len(self._active_agents) >= 2:
            self._start_group_loop()

        self._current_group_name = name
        # Build rich output
        lines = [f"✅ 已载入分组 「{name}」"]
        leader = self._leader
        if leader:
            lines.append(f"👑 领导者: {leader}")
        lines.append(f"📢 发言顺序: {' → '.join(self._active_agents)}")
        lines.append("")
        for a in self._active_agents:
            info = self.registry.get(a, {})
            model = info.get('model', '?')
            badge = " 👑" if leader == a else ""
            lines.append(f"  {a}{badge}: 🤖 {model}")
        return "\n".join(lines)

    def delete_group(self, name: str) -> str:
        """Delete a saved group config."""
        groups = self._load_groups()
        if name not in groups:
            return f"⚠️ 分组 「{name}」 不存在"
        del groups[name]
        self._save_groups(groups)
        return f"🗑 已删除分组 「{name}」"

    def list_groups(self) -> str:
        """List all saved group configs."""
        groups = self._load_groups()
        if not groups:
            return "📋 没有保存的分组\n用 /savegroup 保存当前成员"
        lines = ["📋 已保存的分组：\n"]
        for gname, members in groups.items():
            order = " → ".join(members)
            member_info = []
            for m in members:
                info = self.registry.get(m, {})
                model = info.get('model', '?').split('/')[-1]
                member_info.append(f"{m}({model})")
            lines.append(f"  📁 {gname}")
            lines.append(f"     {' → '.join(member_info)}")
        return "\n".join(lines)

    # ── Response cleanup ───

    def _clean_response(self, content: str, agent_name: str) -> str:
        """Clean up model response: strip think blocks, name prefixes, fake tool calls, etc."""
        import re

        # 1. Strip <think>...</think> blocks (deepseek, some models)
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()

        # 2. Strip Grok-style reasoning prefix (starts with "A: " or similar)
        if content.startswith("A: ") or content.startswith("A："):
            lines = content.split("\n")
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped and not stripped.startswith("A:") and not stripped.startswith("A："):
                    if i > 0 and not lines[i-1].strip():
                        content = "\n".join(lines[i:])
                        break

        # 3. Strip fake/hallucinated tool calls in text
        # Bracket style: [Start search for ...], [Check ...], etc.
        content = re.sub(r"\[(?:Start |Check |Search |Look up |Fetch |查|搜)[^\]]*\]", "", content)
        # XML style: <function_calls>...</function_calls>, <web_search>...</web_search>, <tool>...</tool>, etc.
        content = re.sub(
            r"<(?:function_calls|invoke|web_search|web_fetch|tool|parameter|query|search)[\s\S]*?(?:</(?:function_calls|invoke|web_search|web_fetch|tool|parameter|query|search)>|$)",
            "", content, flags=re.IGNORECASE,
        )

        # 4. Strip ALL agent name prefixes (handles repeated "Benjamin: ..." throughout)
        all_names = list(self.registry.keys())
        for name in all_names:
            for sep in (": ", "：", ":\n"):
                prefix = f"{name}{sep}"
                content = content.replace(prefix, "")
            # Also strip markdown headers like "# Benjamin" or "## Benjamin"
            content = re.sub(rf"^#+\s*{re.escape(name)}\s*$", "", content, flags=re.MULTILINE)

        # 5. Clean up excessive blank lines left after stripping
        content = re.sub(r"\n{3,}", "\n\n", content)

        return content.strip()

    # ── Tool-augmented chat (matching AgentLoop._run_agent_loop) ───

    # Search intent keywords — only pass tools when user message matches
    _SEARCH_KEYWORDS = {
        # Chinese
        "搜索", "搜一下", "查一下", "查找", "查询", "找一下",
        "新闻", "最新", "今天", "今日", "实时", "热点",
        "帮我查", "帮我搜", "帮我找", "上网",
        # English
        "search", "look up", "find", "google", "news",
        "latest", "recent", "today", "current",
    }

    @staticmethod
    def _has_search_intent(messages: list[dict[str, Any]]) -> bool:
        """Check if the latest user message has search intent."""
        # Find the last user message
        for msg in reversed(messages):
            if msg.get("role") == "user":
                text = msg.get("content", "").lower()
                for kw in GroupChatEngine._SEARCH_KEYWORDS:
                    if kw in text:
                        return True
                return False
        return False

    def _get_agent_tools(self, agent_cfg: dict, registry) -> list:
        """Get filtered tool definitions based on agent's per-tool config.

        Config format:
          tools: {web_search: true, exec: false, ...}  # granular
          tools_enabled: true  # legacy: all tools on
        """
        # Granular tools dict
        tools_cfg = agent_cfg.get("tools")
        if isinstance(tools_cfg, dict):
            enabled = {k for k, v in tools_cfg.items() if v}
            if not enabled:
                return []
            return [d for d in registry.get_definitions()
                    if d.get("function", {}).get("name") in enabled]

        # Legacy fallback
        if agent_cfg.get("tools_enabled", False):
            return registry.get_definitions()

        # Default agent (_default flag)
        if agent_cfg.get("_default"):
            return registry.get_definitions()

        return []

    async def _chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        model: str,
        agent_name: str,
        max_iterations: int = 5,
        is_direct: bool = False,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_content_reset: Callable[[], Awaitable[None]] | None = None,
        on_tool_start_override: Callable | None = None,
        on_tool_result_override: Callable | None = None,
        force_no_tools: bool = False,
    ) -> tuple[str, list[str], dict[str, Any]]:
        """Chat with tool calling loop, delegating to the shared tool_loop.

        Returns (content, tools_used, stats).
        """
        from nanobot.agent.tool_loop import tool_loop

        # Tool selection
        agent_cfg = self.registry.get(agent_name, {})
        if is_direct:
            tool_defs = self._get_agent_tools(agent_cfg, self.direct_tools)
        else:
            tool_defs = self._get_agent_tools(agent_cfg, self.tools)

        tool_registry = self.direct_tools if is_direct else self.tools

        # Set chatroom tool context for this agent
        if hasattr(self, '_chatroom_send_tool'):
            self._chatroom_send_tool.set_agent(agent_name)
            self._wait_tool.set_agent(agent_name)
            # Ensure mailbox exists for this agent
            self._mailbox.create(agent_name)

        # Langfuse trace metadata + request log enrichment
        session_id = self._session_dir.name if self._session_dir else "direct"
        trace_metadata = {
            "trace_name": f"{'direct' if is_direct else 'group'}_{agent_name}",
            "trace_user_id": "groupchat",
            "tags": [agent_name, "direct" if is_direct else "group"],
            "generation_name": f"{agent_name}_loop",
            "debug_context": self._debug_context,
            # Fields consumed by _log_request
            "log_agent": agent_name,
            "log_session": session_id,
            "log_topic": self._topic or "",
            "log_mode": "direct" if is_direct else "group",
        }

        # ── Callbacks ──

        _TOOL_ICONS = {
            "web_search": "🔍", "web_fetch": "🌐", "exec": "⚡",
            "read_file": "📖", "write_file": "✏️", "edit_file": "✏️",
            "list_dir": "📁",
        }

        _tool_msg_id: int | None = None  # Track message ID for tool call consolidation
        _tool_msg_text: str = ""  # Store original message text for editing

        async def _on_tool_start(name: str, args: dict) -> None:
            nonlocal _tool_msg_id, _tool_msg_text
            _tool_msg_id = None
            _tool_msg_text = ""
            if not isinstance(args, dict):
                args = {}
            short = (
                args.get("command") or args.get("query")
                or args.get("url") or args.get("path") or ""
            )
            if not short and args:
                short = list(args.values())[0]
            if isinstance(short, str) and len(short) > 80:
                short = short[:80] + "…"
            icon = _TOOL_ICONS.get(name, "🔧")
            text = f"{icon} {agent_name}: {name}({short})"
            _tool_msg_text = text
            # Try to send as editable message
            if self._send_and_get_id_fn:
                _tool_msg_id = await self._send_and_get_id_fn(text)
            elif self._send_fn:
                await self._send(text)

        async def _on_tool_result(name: str, tool_call_id: str, result: str) -> None:
            nonlocal _tool_msg_id, _tool_msg_text
            if not result:
                _tool_msg_id = None
                return
            rlen = len(result)
            preview = result.strip().replace("\n", " ")[:80]
            result_line = f"↳ {preview}{'…' if rlen > 80 else ''} ({rlen}字)"
            # Edit the tool start message to append the result
            if _tool_msg_id and self._edit_fn and _tool_msg_text:
                try:
                    updated = f"{_tool_msg_text}\n{result_line}"
                    await self._edit_fn(_tool_msg_id, updated)
                except Exception:
                    if self._send_fn:
                        await self._send(f"   {result_line}")
            elif self._send_fn:
                await self._send(f"   {result_line}")
            _tool_msg_id = None
            _tool_msg_text = ""

        effective_defs = None if force_no_tools else (tool_defs if tool_defs else None)
        logger.info(
            "_chat_with_tools: agent={} model={} tool_defs={} is_direct={}",
            agent_name, model,
            len(tool_defs) if tool_defs else 0,
            is_direct,
        )

        # Snapshot messages before tool_loop mutates them
        messages_snapshot = []
        for m in messages:
            entry = {"role": m.get("role", "?")}
            if m.get("name"):
                entry["name"] = m["name"]
            content = m.get("content", "")
            if isinstance(content, str):
                entry["content"] = content[:500]
                entry["content_len"] = len(content)
            elif isinstance(content, list):
                # Content blocks (e.g. cache_control)
                text_parts = [b.get("text", "") for b in content if isinstance(b, dict)]
                joined = " ".join(text_parts)
                entry["content"] = joined[:500]
                entry["content_len"] = len(joined)
            else:
                entry["content"] = str(content)[:500] if content else ""
                entry["content_len"] = len(str(content)) if content else 0
            messages_snapshot.append(entry)

        # Capture sampling params
        sampling = dict(getattr(self.provider, "sampling_params", {}))

        # Tool definition names
        tool_names = [d.get("function", {}).get("name", "?") for d in (tool_defs or [])]

        result = await tool_loop(
            provider=self.provider,
            messages=messages,
            tool_registry=tool_registry,
            model=model,
            max_tokens=self.config.max_tokens,
            max_iterations=max_iterations,
            tool_defs=effective_defs,
            metadata=trace_metadata,
            on_tool_start=on_tool_start_override or _on_tool_start,
            on_tool_result=on_tool_result_override or _on_tool_result,
            on_content_delta=on_content_delta,
            on_content_reset=on_content_reset,
            clean_response=lambda c: self._clean_response(c, agent_name),
            result_max_chars=20_000,
        )

        content = result.content or ""
        stats = {
            "iterations": result.iterations,
            "latency": result.latency,
            "tokens": result.token_usage,
            "calls": result.call_details,
            "tool_calls_detail": result.tool_calls_detail,
            "tools_available": result.tools_available,
            "tool_defs_count": len(tool_defs) if tool_defs else 0,
            "tool_names": tool_names,
            "messages_snapshot": messages_snapshot,
            "sampling_params": sampling,
            "max_tokens": self.config.max_tokens,
            "status_code": result.status_code,
            "finish_reason": result.finish_reason,
        }
        return content, result.tools_used, stats

    async def direct_chat(self, user_message: str) -> str | None:
        """Send message to single active agent (1-on-1 mode).

        Uses proper multi-message format (like SillyTavern) with tool calling
        matching nanobot core agent logic.
        """
        if len(self._active_agents) != 1:
            return None

        agent_name = self._active_agents[0]
        agent = self.registry[agent_name]

        # Build messages matching SillyTavern's minimal flow:
        # system(persona) → [history as user/assistant] → user(new)
        now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": agent["prompt"] + f"\n\n[Current date and time: {now}]"},
        ]

        # Few-shot examples (only if EXAMPLES.md exists)
        examples = agent.get("examples", "")
        if examples:
            messages.append({"role": "system", "content": f"以下是对话风格示例：\n{examples}"})

        # Chat history as proper alternating messages
        messages.extend(self._history_to_messages(agent_name))

        # Current user message
        messages.append({"role": "user", "content": user_message})

        # Post-history instructions only if explicitly defined
        instructions = agent.get("instructions", "")
        if instructions:
            messages.append({"role": "system", "content": instructions})

        # ── Streaming state ──
        _stream_msg_id: int | None = None
        _stream_buffer: list[str] = []
        _last_edit: float = 0.0
        _EDIT_INTERVAL = 0.8
        _header = f"💬 {agent_name}:\n\n"

        async def _on_delta(delta: str) -> None:
            nonlocal _stream_msg_id, _last_edit
            _stream_buffer.append(delta)
            now_t = _time.time()

            if _stream_msg_id is None and self._send_and_get_id_fn:
                text = _header + "".join(_stream_buffer) + " ▍"
                _stream_msg_id = await self._send_and_get_id_fn(text)
                _last_edit = now_t
            elif _stream_msg_id and self._edit_fn and (now_t - _last_edit) >= _EDIT_INTERVAL:
                text = _header + "".join(_stream_buffer) + " ▍"
                try:
                    await self._edit_fn(_stream_msg_id, text)
                except Exception:
                    pass
                _last_edit = now_t

        async def _on_reset() -> None:
            """Clear stream buffer when tool calls interrupt mid-stream."""
            _stream_buffer.clear()

        _delta_cb = _on_delta if (self._edit_fn and self._send_and_get_id_fn) else None
        _reset_cb = _on_reset if _delta_cb else None

        try:
            content, tools_used, stats = await self._chat_with_tools(
                messages=messages,
                model=agent["model"],
                agent_name=agent_name,
                is_direct=True,
                on_content_delta=_delta_cb,
                on_content_reset=_reset_cb,
            )
            self._request_log.append({
                "agent": agent_name, "model": agent["model"],
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": len(content), "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "direct", "tools": tools_used,
                "input_preview": user_message[:200],
                "output": content[:500],
                **stats,
            })
            if content:
                self._add_message("用户", user_message)
                self._add_message(agent_name, content)
                final_text = f"{_header}{content}"
                if _stream_msg_id and self._edit_fn:
                    # Final edit — remove cursor
                    try:
                        await self._edit_fn(_stream_msg_id, final_text)
                    except Exception:
                        await self._send(final_text)
                    return None  # Already sent via streaming
                else:
                    return final_text
            else:
                if _stream_msg_id and self._edit_fn:
                    try:
                        await self._edit_fn(_stream_msg_id, f"⚠️ {agent_name} 返回空回复")
                    except Exception:
                        pass
                    return None
                return f"⚠️ {agent_name} 返回空回复 (模型可能暂时异常，请重试)"
        except Exception as e:
            logger.error("Direct chat with {} failed: {}", agent_name, e)
            self._request_log.append({
                "agent": agent_name, "model": agent["model"],
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": 0, "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "direct", "error": str(e),
            })
            return f"⚠️ {agent_name} 回复失败: {e}"

    def inject(self, message: str) -> None:
        """Inject a user message into the group chat."""
        # Lazy start: if 2+ agents and loop not running, start it now
        if not self._running and len(self._active_agents) >= 2:
            self._start_group_loop()
        if self._running:
            self._input_queue.put_nowait(message)

    def request_summary(self) -> None:
        """Request a discussion summary."""
        if self._running:
            self._input_queue.put_nowait("__SUMMARY__")

    def stop(self) -> None:
        """Stop everything and clear active agents."""
        self._stop_group_loop()
        self._active_agents.clear()
        self._save_active()

    # ── Internal ─────────────────────────────────────────────

    def _resolve_agent_name(self, name: str) -> str | None:
        """Case-insensitive agent name resolution."""
        for reg_name in self.registry:
            if reg_name.lower() == name.lower():
                return reg_name
        return None

    def _start_group_loop(self) -> None:
        """Start the async group chat loop."""
        # Always cancel any prior task to avoid duplicate loops
        if self._task and not self._task.done():
            self._task.cancel()
        if self._running:
            return
        self._running = True
        self._input_queue = asyncio.Queue()
        if not self._topic:
            self._topic = "自由讨论"

        # Session directory
        timestamp = _cn_now().strftime("%Y%m%d-%H%M%S")
        sessions_dir = Path.home() / ".nanobot" / "collab-sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = sessions_dir / f"gc-{timestamp}"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        self._task = asyncio.create_task(self._run_loop())
        logger.info("Group chat loop started with {}", self._active_agents)

    def _stop_group_loop(self) -> None:
        """Stop the group chat loop (keeps active agents and history)."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _send(self, text: str) -> None:
        if self._send_fn:
            try:
                await self._send_fn(text)
            except Exception as e:
                logger.error("Groupchat send failed: {}", e)

    def _add_message(self, sender: str, content: str) -> None:
        self._history.append({"sender": sender, "content": content})
        if len(self._history) > self.config.max_history:
            self._history = self._history[-self.config.max_history:]
        if self._session_dir:
            with open(self._session_dir / "chat_log.txt", "a") as f:
                f.write(f"[{sender}]: {content}\n---\n")

    def _format_history(self) -> str:
        return "\n\n".join(f"[{m['sender']}]: {m['content']}" for m in self._history)

    def _history_to_messages(self, current_agent: str = "") -> list[dict[str, Any]]:
        """Convert history into proper API messages (SillyTavern style).

        Uses `name` field + content prefix for attribution,
        matching SillyTavern's DEFAULT character_names_behavior.
        """
        msgs: list[dict[str, Any]] = []
        for m in self._history:
            sender = m["sender"]
            content = m["content"]
            if sender == "用户":
                msgs.append({"role": "user", "content": content})
            elif sender == "系统":
                msgs.append({"role": "system", "content": content})
            else:
                # SillyTavern DEFAULT: name field + content prefix
                msgs.append({
                    "role": "assistant",
                    "content": f"{sender}: {content}",
                    "name": sender.replace(" ", "_"),
                })
        return msgs

    def _pick_next_speaker(self, last_content: str = "") -> str:
        names = self._active_agents
        # @mentions
        for name in names:
            if f"@{name}" in last_content or f"@{name.lower()}" in last_content:
                return name
        # Implicit mentions
        mentioned = [n for n in names if n.lower() in last_content.lower()
                     and (not self._history or self._history[-1]["sender"] != n)]
        if mentioned:
            return random.choice(mentioned)
        # Avoid repeat
        candidates = list(names)
        if self._history:
            last_speaker = self._history[-1]["sender"]
            candidates = [n for n in candidates if n != last_speaker]
        return random.choice(candidates) if candidates else random.choice(names)

    def _build_agent_prompt(self, agent_name: str) -> list[dict[str, Any]]:
        """Build prompt with configurable component order.

        Component order is configurable via /prompt command (global).
        Template overrides use {{agent}}, {{members}}, {{datetime}}, etc.
        Overrides stored in ~/.nanobot/prompt_overrides.json under __global__ key.
        """
        agent = self.registry[agent_name]
        order = self.get_agent_prompt_order()

        # Load global template overrides
        overrides = self._load_prompt_overrides("__global__")

        # Template variables for expansion
        members_list = ", ".join(self._active_agents) if self._active_agents else "(无)"
        other_members = [a for a in self._active_agents if a != agent_name]
        now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
        tool_names = "web_search, web_fetch, exec, read_file, write_file, edit_file, list_dir"
        tpl_vars = {
            "{{agent}}": agent_name,
            "{{members}}": members_list,
            "{{datetime}}": now,
            "{{round}}": str(self._round),
            "{{tools}}": tool_names,
            "{{others}}": ", ".join(other_members),
        }

        messages: list[dict[str, Any]] = []
        for key in order:
            if key == "history":
                messages.extend(self._history_to_messages(agent_name))
                continue
            if key == "leader_prompt" and self._leader != agent_name:
                continue

            # Check for global override first, then default content
            override = overrides.get(key)
            if override:
                content = self._expand_template_vars(override, tpl_vars)
            else:
                content = self._get_component_content(agent_name, agent, key)
            if not content:
                continue
            # Wrap examples with label
            if key == "examples":
                content = f"[Example Chat]\n{content}"
            messages.append({"role": "system", "content": content})

        return messages

    @staticmethod
    def _expand_template_vars(text: str, tpl_vars: dict[str, str]) -> str:
        """Expand template variables like {{agent}}, {{members}}, etc."""
        for key, val in tpl_vars.items():
            text = text.replace(key, val)
        return text

    @staticmethod
    def _load_prompt_overrides(agent_name: str) -> dict[str, str]:
        """Load template overrides from prompt_overrides.json."""
        f = Path.home() / ".nanobot" / "prompt_overrides.json"
        if f.exists():
            try:
                data = json.loads(f.read_text())
                return data.get(agent_name, {})
            except Exception:
                pass
        return {}

    async def _agent_speak(
        self,
        agent_name: str,
        synthesis_context: str | None = None,
        no_tools: bool = False,
    ) -> tuple[str, list[str], dict] | None:
        """Run one agent's turn. Returns (content, tools_used, stats) or None on error.

        Args:
            synthesis_context: Optional research summary injected before the
                agent's own prompt (used for leader synthesis in parallel mode).
            no_tools: If True, disable tool calling (forces pure text response).
                Useful for synthesis/discussion phases.
        """
        if agent_name not in self.registry:
            return None
        model = self.registry[agent_name]["model"]
        messages = self._build_agent_prompt(agent_name)

        # Inject synthesis context for leader (before the final nudge)
        if synthesis_context:
            # Insert before the last message (group_nudge) so the model sees
            # the research results right before being asked to respond.
            insert_pos = max(len(messages) - 1, 0)
            messages.insert(insert_pos, {
                "role": "system",
                "content": synthesis_context,
            })

        # ── Context size breakdown (only when /debug enabled) ──
        if self._debug_context:
            total_chars = 0
            parts: list[str] = []
            for i, msg in enumerate(messages):
                role = msg.get("role", "?")
                name = msg.get("name", "")
                content = msg.get("content", "")
                c_len = len(content) if isinstance(content, str) else sum(
                    len(b.get("text", "")) for b in content if isinstance(b, dict)
                ) if isinstance(content, list) else 0
                total_chars += c_len
                label = (content[:30] if isinstance(content, str) else "").replace("\n", " ")
                tag = f"{name}:" if name else ""
                parts.append(f"  [{i}] {role}{':' if tag else ''}{tag} {c_len:,}字 | {label}…")
            logger.info(
                "Context for {} ({}):\n{}\n  ── TOTAL: {:,} chars, {} messages",
                agent_name, model, "\n".join(parts), total_chars, len(messages),
            )

        # ── Streaming state ──
        _stream_msg_id: int | None = None
        _stream_buffer: list[str] = []
        _last_edit: float = 0.0
        _EDIT_INTERVAL = 0.8  # Telegram rate-limit safe interval

        total = len(self._active_agents)
        idx = self._active_agents.index(agent_name) + 1 if agent_name in self._active_agents else 0
        badge = " 👑" if self._leader == agent_name else ""
        round_tag = f" [{idx}/{total}]" if total > 1 else ""
        _header = f"💬 {agent_name}{badge}{round_tag}:\n\n"

        async def _on_delta(delta: str) -> None:
            nonlocal _stream_msg_id, _last_edit
            _stream_buffer.append(delta)
            now = _time.time()

            if _stream_msg_id is None and self._send_and_get_id_fn:
                # First chunk — send initial message
                text = _header + "".join(_stream_buffer) + " ▍"
                _stream_msg_id = await self._send_and_get_id_fn(text)
                _last_edit = now
            elif _stream_msg_id and self._edit_fn and (now - _last_edit) >= _EDIT_INTERVAL:
                # Throttled edit
                text = _header + "".join(_stream_buffer) + " ▍"
                try:
                    await self._edit_fn(_stream_msg_id, text)
                except Exception:
                    pass  # Telegram may reject if text unchanged
                _last_edit = now

        async def _on_reset() -> None:
            """Clear stream buffer when tool calls interrupt mid-stream."""
            _stream_buffer.clear()
            # Update Telegram message to show tool status instead of stale content
            if _stream_msg_id and self._edit_fn:
                try:
                    await self._edit_fn(_stream_msg_id, f"{_header}🔧 使用工具中...")
                except Exception:
                    pass

        # Only enable streaming if edit callbacks are available
        _delta_cb = _on_delta if (self._edit_fn and self._send_and_get_id_fn) else None
        _reset_cb = _on_reset if _delta_cb else None

        try:
            content, tools_used, stats = await self._chat_with_tools(
                messages=messages,
                model=model,
                agent_name=agent_name,
                max_iterations=1 if no_tools else 5,
                on_content_delta=_delta_cb,
                on_content_reset=_reset_cb,
                force_no_tools=no_tools,
            )
            iters = stats.get("iterations", 1)
            latency = stats.get("latency", 0)
            is_error = stats.get("finish_reason") == "error"

            if is_error:
                # ── Error: do NOT add to history, show warning ──
                err_short = content[:150] if content else "Unknown error"
                logger.error("Agent {} LLM error ({}s): {}", agent_name, latency, err_short)
                err_msg = f"⚠️ {agent_name} 请求失败 ({latency}s): {err_short}"
                if _stream_msg_id and self._edit_fn:
                    try:
                        await self._edit_fn(_stream_msg_id, err_msg)
                    except Exception:
                        await self._send(err_msg)
                else:
                    await self._send(err_msg)
                self._request_log.append({
                    "agent": agent_name, "model": model,
                    "msgs": len(messages), "max_tokens": self.config.max_tokens,
                    "reply_len": 0, "time": _cn_now().strftime("%H:%M:%S"),
                    "mode": "group", "error": err_short,
                    "status_code": stats.get("status_code"),
                    **{k: v for k, v in stats.items() if k not in ("status_code",)},
                })
                return None

            if tools_used:
                completion_msg = f"✅ {agent_name} 完成 ({latency}s, {iters}次迭代, 工具: {', '.join(tools_used)})"
            elif latency > 0:
                completion_msg = f"✅ {agent_name} 完成 ({latency}s)"
            else:
                completion_msg = ""

            self._request_log.append({
                "agent": agent_name, "model": model,
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": len(content), "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "group", "tools": tools_used,
                "input_preview": (self._history[-2]["content"][:200] if len(self._history) >= 2 else ""),
                "output": content[:500],
                **stats,
            })
            logger.info(
                "Agent {} result: content_len={} tools={} stream_msg_id={} iters={} latency={}",
                agent_name, len(content), tools_used, _stream_msg_id,
                stats.get("iterations"), stats.get("latency"),
            )

            # ── Final display: edit streamed message FIRST, then send completion ──
            if content:
                self._add_message(agent_name, content)
                final_text = f"{_header}{content}"
                if _stream_msg_id and self._edit_fn:
                    try:
                        await self._edit_fn(_stream_msg_id, final_text[:4096])
                        logger.info("Agent {}: final streamed edit OK", agent_name)
                    except Exception as edit_err:
                        logger.warning("Final stream edit failed for {}: {}", agent_name, edit_err)
                        await self._send(final_text[:4096])
                else:
                    logger.info("Agent {}: sending via _send (len={})", agent_name, len(final_text))
                    await self._send(final_text[:4096])
            elif _stream_msg_id and self._edit_fn:
                try:
                    await self._edit_fn(_stream_msg_id, f"{_header}(空回复)")
                except Exception:
                    pass
                logger.warning("Agent {} returned empty content (streamed placeholder updated)", agent_name)
            else:
                logger.warning("Agent {} returned empty content, content repr: {!r}", agent_name, content)

            # Send completion notification AFTER the final text is displayed
            if completion_msg:
                await self._send(completion_msg)
            return (content, tools_used, stats)
        except Exception as e:
            logger.error("Groupchat: {} LLM call failed: {}", agent_name, e)
            self._request_log.append({
                "agent": agent_name, "model": model,
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": 0, "time": _cn_now().strftime("%H:%M:%S"),
                "mode": "group", "error": str(e),
            })
            await self._send(f"⚠️ {agent_name} 回复失败: {e}")
            return None

    # ── Parallel Leader Mode (Orchestra) ─────────────────────────────

    async def _orchestra_round(self, speak_order: list[str]) -> None:
        """Run non-leader agents in parallel with Grok-style display,
        then leader synthesizes all findings.

        Visual style matches xAI's multi-agent output:
        - Leader header: "👑 {Name}领导者"
        - Agent sections: "Agent N (Name)" with tool activity
        - Tool format: "已搜索的网络 query" / "已浏览 url"
        - Consolidated per-agent editable messages
        """
        leader = self._leader
        others = [a for a in speak_order if a != leader]

        if not others or not leader:
            return

        total = len(speak_order)

        # ── Phase 0: Leader announcement ──
        await self._send(
            f"👑 {leader}领导者\n"
            f"正在分析任务并协调 {len(others)} 个 Agent 并行工作..."
        )

        async def _run_agent_grok(
            name: str, agent_idx: int,
        ) -> tuple[str, tuple[str, list[str], dict] | None]:
            """Run one agent with Grok-style consolidated display."""
            if name not in self.registry:
                return (name, None)

            agent_cfg = self.registry[name]
            model = agent_cfg["model"]
            model_short = model.split("/")[-1]
            messages = self._build_agent_prompt(name)

            # ── Consolidated editable message for this agent ──
            _lines: list[str] = []       # tool activity lines
            _msg_id: int | None = None
            _header = f"Agent {agent_idx + 1} ({name})"

            if self._send_and_get_id_fn:
                _msg_id = await self._send_and_get_id_fn(
                    f"{_header}\n⏳ 思考中... ({model_short})"
                )

            async def _edit_consolidated() -> None:
                """Re-render the consolidated message."""
                if not (_msg_id and self._edit_fn):
                    return
                text = f"{_header}\n" + "\n\n".join(_lines)
                try:
                    await self._edit_fn(_msg_id, text[:4096])
                except Exception:
                    pass

            # ── Grok-style tool callbacks ──

            async def _on_tool_start(tool_name: str, args: dict) -> None:
                if not isinstance(args, dict):
                    args = {}
                if tool_name == "web_search":
                    query = args.get("query", "")
                    _lines.append(f"已搜索的网络\n{query}")
                elif tool_name == "web_fetch":
                    url = args.get("url", "")
                    short = url[:60] + ("..." if len(url) > 60 else "")
                    _lines.append(f"已浏览\n{short}")
                elif tool_name == "exec":
                    cmd = (args.get("command", "") or "")[:50]
                    _lines.append(f"⚡ {cmd}")
                else:
                    short = ""
                    if args:
                        first = list(args.values())[0]
                        if isinstance(first, str):
                            short = first[:40]
                    _lines.append(f"🔧 {tool_name}" + (f" {short}" if short else ""))
                await _edit_consolidated()

            async def _on_tool_result(
                tool_name: str, tool_call_id: str, result: str,
            ) -> None:
                if not result or not _lines:
                    return
                rlen = len(result)
                last = _lines[-1]
                if tool_name == "web_search":
                    # Parse actual result count from the output format:
                    # "Results for: query  (N results)"
                    import re as _re
                    m = _re.search(r'\((\d+) results?\)', result[:100])
                    count = int(m.group(1)) if m else max(result.count("\n") // 3, 1)
                    _lines[-1] = f"{last}\n{count} 条结果"
                elif tool_name == "web_fetch":
                    _lines[-1] = f"{last}\n({rlen}字)"
                else:
                    preview = result.strip().replace("\n", " ")[:60]
                    _lines[-1] = f"{last}\n↳ {preview}{'…' if rlen > 60 else ''}"
                await _edit_consolidated()

            # ── Streaming callback (accumulate text) ──
            _stream_buf: list[str] = []
            _last_edit: float = 0.0

            async def _on_delta(delta: str) -> None:
                nonlocal _last_edit
                _stream_buf.append(delta)
                now = _time.time()
                if (_msg_id and self._edit_fn
                        and (now - _last_edit) >= 0.8):
                    activity = "\n\n".join(_lines) + "\n\n" if _lines else ""
                    text = f"{_header}\n{activity}" + "".join(_stream_buf) + " ▍"
                    try:
                        await self._edit_fn(_msg_id, text[:4096])
                    except Exception:
                        pass
                    _last_edit = now

            async def _on_reset() -> None:
                _stream_buf.clear()

            _delta_cb = _on_delta if (self._edit_fn and self._send_and_get_id_fn) else None
            _reset_cb = _on_reset if _delta_cb else None

            # ── Run LLM with tools (pass Grok-style callbacks) ──
            try:
                content, tools_used, stats = await self._chat_with_tools(
                    messages=messages,
                    model=model,
                    agent_name=name,
                    max_iterations=5,
                    on_content_delta=_delta_cb,
                    on_content_reset=_reset_cb,
                    on_tool_start_override=_on_tool_start,
                    on_tool_result_override=_on_tool_result,
                )
                is_error = stats.get("finish_reason") == "error"
                latency = stats.get("latency", 0)

                if is_error:
                    err_short = content[:150] if content else "Unknown error"
                    if _msg_id and self._edit_fn:
                        try:
                            await self._edit_fn(
                                _msg_id, f"{_header}\n⚠️ 失败 ({latency}s): {err_short}"
                            )
                        except Exception:
                            pass
                    self._request_log.append({
                        "agent": name, "model": model,
                        "reply_len": 0, "time": _cn_now().strftime("%H:%M:%S"),
                        "mode": "orchestra", "error": err_short, **stats,
                    })
                    return (name, None)

                # ── Final consolidated display ──
                activity_text = "\n\n".join(_lines) if _lines else ""
                if content:
                    self._add_message(name, content)
                    sep = "\n\n" if activity_text else ""
                    final = f"{_header}\n{activity_text}{sep}{content}"
                    if _msg_id and self._edit_fn:
                        try:
                            await self._edit_fn(_msg_id, final[:4096])
                        except Exception:
                            await self._send(final[:4096])
                    else:
                        await self._send(final[:4096])
                elif _msg_id and self._edit_fn:
                    try:
                        await self._edit_fn(
                            _msg_id, f"{_header}\n{activity_text}\n\n(空回复)" if activity_text
                            else f"{_header}\n(空回复)"
                        )
                    except Exception:
                        pass

                self._request_log.append({
                    "agent": name, "model": model,
                    "reply_len": len(content), "time": _cn_now().strftime("%H:%M:%S"),
                    "mode": "orchestra", "tools": tools_used, **stats,
                })
                return (name, (content, tools_used, stats))

            except Exception as e:
                logger.error("Orchestra: {} failed: {}", name, e)
                if _msg_id and self._edit_fn:
                    try:
                        await self._edit_fn(_msg_id, f"{_header}\n⚠️ 失败: {e}")
                    except Exception:
                        pass
                self._request_log.append({
                    "agent": name, "model": model,
                    "reply_len": 0, "time": _cn_now().strftime("%H:%M:%S"),
                    "mode": "orchestra", "error": str(e),
                })
                return (name, None)

        # ── Run all non-leader agents concurrently ──
        tasks = [_run_agent_grok(name, si) for si, name in enumerate(others)]
        parallel_results = await asyncio.gather(*tasks, return_exceptions=True)

        # ── Phase 2: Build synthesis context ──
        research_parts: list[str] = []
        for item in parallel_results:
            if isinstance(item, Exception):
                logger.error("Orchestra parallel agent error: {}", item)
                continue
            name, result = item
            if result is None:
                research_parts.append(f"[{name}]: (请求失败，无结果)")
                continue
            content, tools_used, stats = result
            tool_str = f" | 工具: {', '.join(tools_used)}" if tools_used else ""
            tool_details = stats.get("tool_calls_detail", [])
            detail_lines = ""
            if tool_details:
                details = []
                for td in tool_details[:8]:
                    t_name = td.get("name", "?")
                    t_result = td.get("result_preview", "")[:3000]
                    details.append(f"  - {t_name}: {t_result}")
                detail_lines = "\n" + "\n".join(details)
            research_parts.append(
                f"[{name}]{tool_str}:\n{content or '(空回复)'}{detail_lines}"
            )

        synthesis_context = (
            "[团队研究结果 — 请综合以下所有 agent 的发现，给出最终回复]\n"
            "[重要指令：\n"
            "1. 直接基于以下 agent 报告中的信息综合回答，不要再调用任何工具。\n"
            "2. 所有具体的版本号、日期、数值必须来自下面的 agent 报告。\n"
            "3. 优先引用日期最新的信息，如果多个 agent 报告了不同版本，以最新的为准。\n"
            "4. 信息不完整时如实说明即可。]\n\n"
            + "\n\n---\n\n".join(research_parts)
        )
        logger.info(
            "Orchestra synthesis context: {} agents, {} chars",
            len(research_parts), len(synthesis_context),
        )

        # ── Phase 3: Leader synthesizes ──
        await self._send(
            f"\n👑 {leader}领导者\n"
            f"正在综合 {len(others)} 个 Agent 的研究结果..."
        )
        await self._agent_speak(leader, synthesis_context=synthesis_context)

    async def _generate_summary(self) -> None:
        if not self._history:
            return
        # Use first active agent's model
        agent_name = self._active_agents[0] if self._active_agents else list(self.registry.keys())[0]
        model = self.registry[agent_name]["model"]

        try:
            response = await self.provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": "你是一个讨论总结专家。"},
                    {"role": "user", "content": (
                        f"话题：{self._topic}\n\n"
                        f"群聊记录：\n{self._format_history()}\n\n"
                        "请输出简洁总结：1)核心观点 2)分歧点 3)初步结论"
                    )},
                ],
                model=model,
                max_tokens=2000,
            )
            summary = response.content or "无法生成总结"
            await self._send(f"📋 讨论总结:\n\n{summary}")
        except Exception as e:
            logger.error("Summary failed: {}", e)
            await self._send(f"⚠️ 总结生成失败: {e}")

    async def _run_loop(self) -> None:
        """Main group chat loop — runs while 2+ agents are active."""
        _my_task = asyncio.current_task()
        try:
            await self._send(
                f"🎭 群聊模式！\n"
                f"👥 成员: {', '.join(self._active_agents)}\n"
                f"📌 直接发消息，所有 agent 会轮流回复"
            )

            if not any(m["sender"] == "系统" for m in self._history):
                self._add_message("系统", f"话题：{self._topic}")

            rounds = 0
            while self._running and rounds < self.config.max_rounds:
                rounds += 1

                # Wait for user input (block until user sends something)
                user_input = None
                while self._running:
                    try:
                        user_input = await asyncio.wait_for(self._input_queue.get(), timeout=1.0)
                        break
                    except asyncio.TimeoutError:
                        continue

                if not self._running or not user_input:
                    break

                if user_input == "__SUMMARY__":
                    await self._generate_summary()
                    continue

                # Record user message (no echo)
                self._add_message("用户", user_input)
                self._round = rounds

                # Determine speaking order
                current_agents = list(self._active_agents)
                if self._leader and self._leader in current_agents:
                    # Leader mode: others first, leader last
                    others = [a for a in current_agents if a != self._leader]
                    speak_order = others + [self._leader]
                else:
                    speak_order = current_agents

                # Parallel mode when leader is set (orchestra)
                if self._leader and self._leader in current_agents and len(others) > 0:
                    await self._orchestra_round(speak_order)
                elif self._mode == "broadcast" and not self._leader:
                    # Broadcast mode: all agents concurrently, out-of-order display
                    from nanobot.groupchat.broadcast import broadcast_round
                    await broadcast_round(speak_order, self, self._mailbox)
                else:
                    # Serial mode (no leader)
                    for si, name in enumerate(speak_order):
                        if not self._running or name not in self._active_agents:
                            break
                        badge = " 👑" if self._leader == name else ""
                        model_short = self.registry.get(name, {}).get("model", "?").split("/")[-1]
                        await self._send(f"⏳ {name}{badge} 思考中... ({model_short}) [{si+1}/{len(speak_order)}]")
                        await asyncio.sleep(self.config.auto_reply_delay)
                        await self._agent_speak(name)

                # Signal round complete (e.g. stop typing indicator)
                if self._on_round_done:
                    try:
                        await self._on_round_done()
                    except Exception:
                        pass

            if self._running:
                await self._send("🔚 群聊结束！正在生成总结...")
                await self._generate_summary()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Group chat loop error: {}", e)
            await self._send(f"❌ 群聊异常: {e}")
        finally:
            # Only reset _running if we are still the active loop.
            # A replacement loop (via /loadgroup after /new) may have already
            # set _running = True — don't clobber it.
            if self._task is _my_task:
                self._running = False
            logger.info("Group chat loop ended")
