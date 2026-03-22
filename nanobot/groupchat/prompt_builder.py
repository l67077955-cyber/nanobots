"""Prompt construction for group chat agents.

Extracts all prompt-related logic from GroupChatEngine:
- Component ordering and management
- Template expansion
- Per-agent and global overrides
- History → messages conversion
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.groupchat.utils import cn_now as _cn_now


# ── Constants ─────────────────────────────────────────────────

DEFAULT_PROMPT_ORDER = [
    "main_prompt", "group_context", "persona",
    "tool_instructions", "broadcast_hint", "examples",
    "history", "instructions", "leader_prompt", "group_nudge",
]

COMPONENT_LABELS = {
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

GLOBAL_EDITABLE = {
    "main_prompt", "group_context", "tool_instructions", "broadcast_hint",
    "examples", "instructions", "leader_prompt", "group_nudge",
}
AGENT_EDITABLE = {"persona"}
EDITABLE_COMPONENTS = GLOBAL_EDITABLE | AGENT_EDITABLE


# ── Template Defaults ─────────────────────────────────────────

TEMPLATES: dict[str, str] = {
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
        "## ⚡ 批量调用（重要！）\n"
        "尽量在一次回复中同时调用多个工具，减少往返次数。\n"
        "✅ 好: 一次返回 web_search(中文关键词) + web_search(English keywords)\n"
        "❌ 差: 先搜中文 → 等结果 → 再搜英文 → 等结果\n"
        "能并行的工具一定要同时调用！\n\n"
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
        "[广播模式 — 多Agent协作]\n"
        "你是 {{agent_idx}}/{{total}} 号成员，代号 {{agent}}\n"
        "队友: {{teammates}}\n\n"
        "用户请求: {{user_question}}\n\n"
        "## 你的工具\n"
        "- chatroom_send(to, message): 给队友发消息。to 可以是具体名字或 \"All\"\n"
        "- wait(): 等待队友消息\n\n"
        "## 发言顺序\n"
        "系统使用对话资源池控制消息量：每条消息消耗槽位（发All=3，发个人=1）。\n"
        "池满时 chatroom_send 会阻塞，直到有人 wait() 释放槽位。\n\n"
        "## 协作方式\n"
        "1. 先独立完成你的工作（思考/搜索/分析）\n"
        "2. 用 chatroom_send 分享你的观点或发现\n"
        "3. 用 wait() 听队友的消息\n"
        "4. 自己判断：要回复就 chatroom_send，可以连续发多条，也可以沉默观察后再 wait()\n"
        "5. 当所有人都在 wait 时，系统会自动结束本轮讨论\n\n"
        "## 限制\n"
        "- 禁止一上来就 wait，必须先做工作再发言\n"
        "- 网络调用（web_search + web_fetch）最多 3 次"
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


# ── PromptBuilder Class ───────────────────────────────────────

class PromptBuilder:
    """Builds agent prompts from configurable components.

    Decoupled from GroupChatEngine — receives state through a
    lightweight context interface.
    """

    def __init__(self, *, config: Any, workspace: Path):
        self._config = config
        self._workspace = workspace
        self._prompt_order: dict[str, list[str]] = self._load_prompt_order()

    # ── Order management ──

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
        return list(self._prompt_order.get("default", DEFAULT_PROMPT_ORDER))

    def set_default_prompt_order(self, order: list[str]) -> None:
        self._prompt_order["default"] = order
        self._save_prompt_order()

    def remove_prompt_component(self, idx: int) -> str:
        order = self.get_agent_prompt_order()
        if idx < 0 or idx >= len(order):
            return "❌ 无效索引"
        key = order[idx]
        if key == "history":
            return "❌ 聊天记录 (history) 不可删除"
        label = COMPONENT_LABELS.get(key, key)
        order.pop(idx)
        self.set_default_prompt_order(order)
        return f"🗑 已移除: {label}"

    def get_available_components(self) -> list[str]:
        current = set(self.get_agent_prompt_order())
        return [k for k in DEFAULT_PROMPT_ORDER if k not in current]

    # ── Component content ──

    def get_prompt_components(
        self,
        agent_name: str,
        registry: dict[str, dict],
        active_agents: list[str],
    ) -> list[dict[str, Any]]:
        agent = registry.get(agent_name, {})
        order = self.get_agent_prompt_order(agent_name)
        components = []
        for key in order:
            content = self._get_component_content(agent_name, agent, key, active_agents)
            components.append({
                "key": key,
                "label": COMPONENT_LABELS.get(key, key),
                "content": content,
                "chars": len(content) if content else 0,
                "editable": key in EDITABLE_COMPONENTS,
            })
        return components

    def _get_component_content(
        self,
        agent_name: str,
        agent: dict,
        key: str,
        active_agents: list[str],
        leader: str | None = None,
    ) -> str:
        if key == "main_prompt":
            return (
                f"Write {agent_name}'s next reply in a fictional group chat. "
                f"Write 1 reply only in character as {agent_name}. "
                f"Do not write as or for other characters."
            )
        elif key == "group_context":
            members = ", ".join(active_agents) if active_agents else "(无)"
            now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
            return f"[Start a new group chat. Group members: {members}]\n[Current date and time: {now}]"
        elif key == "persona":
            return agent.get("prompt", "")
        elif key == "tool_instructions":
            return ""
        elif key == "examples":
            return agent.get("examples", "")
        elif key == "history":
            return "[聊天记录 — 动态插入]"
        elif key == "instructions":
            return agent.get("instructions", "")
        elif key == "leader_prompt":
            if leader == agent_name:
                return "[Leader prompt — 自动生成]"
            return ""
        elif key in ("broadcast_hint", "group_nudge"):
            return ""
        return ""

    @staticmethod
    def get_component_template(key: str) -> str:
        return TEMPLATES.get(key, "")

    # ── Prompt building ──

    def build_agent_prompt(
        self,
        agent_name: str,
        *,
        registry: dict[str, dict],
        active_agents: list[str],
        history: list[dict[str, str]],
        leader: str | None = None,
        round_num: int = 0,
    ) -> list[dict[str, Any]]:
        """Build the full prompt messages list for an agent turn."""
        agent = registry[agent_name]
        order = self.get_agent_prompt_order()

        overrides = self._load_prompt_overrides("__global__")

        members_list = ", ".join(active_agents) if active_agents else "(无)"
        other_members = [a for a in active_agents if a != agent_name]
        now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
        tool_names = "web_search, web_fetch, exec, read_file, write_file, edit_file, list_dir"
        tpl_vars = {
            "{{agent}}": agent_name,
            "{{members}}": members_list,
            "{{datetime}}": now,
            "{{round}}": str(round_num),
            "{{tools}}": tool_names,
            "{{others}}": ", ".join(other_members),
        }

        messages: list[dict[str, Any]] = []
        for key in order:
            if key == "history":
                messages.extend(self.history_to_messages(history, agent_name))
                continue
            if key == "leader_prompt" and leader != agent_name:
                continue

            override = overrides.get(key)
            if override:
                content = self._expand_template_vars(override, tpl_vars)
            else:
                content = self._get_component_content(
                    agent_name, agent, key, active_agents, leader
                )
            if not content:
                continue
            if key == "examples":
                content = f"[Example Chat]\n{content}"
            messages.append({"role": "system", "content": content})

        return messages

    @staticmethod
    def history_to_messages(
        history: list[dict[str, str]],
        current_agent: str = "",
    ) -> list[dict[str, Any]]:
        """Convert history dicts into LLM API messages."""
        msgs: list[dict[str, Any]] = []
        for m in history:
            sender = m["sender"]
            content = m["content"]
            if sender == "用户":
                msgs.append({"role": "user", "content": content})
            elif sender == "系统":
                msgs.append({"role": "system", "content": content})
            else:
                msgs.append({
                    "role": "assistant",
                    "content": f"{sender}: {content}",
                    "name": sender.replace(" ", "_"),
                })
        return msgs

    @staticmethod
    def _expand_template_vars(text: str, tpl_vars: dict[str, str]) -> str:
        for key, val in tpl_vars.items():
            text = text.replace(key, val)
        return text

    @staticmethod
    def _load_prompt_overrides(agent_name: str) -> dict[str, str]:
        f = Path.home() / ".nanobot" / "prompt_overrides.json"
        if f.exists():
            try:
                data = json.loads(f.read_text())
                return data.get(agent_name, {})
            except Exception:
                pass
        return {}

    # ── Component updates ──

    def update_prompt_component(
        self,
        agent_name: str,
        key: str,
        content: str,
        registry: dict[str, dict],
        workspace: Path,
        agents_dir: Path | None = None,
    ) -> str:
        if key not in EDITABLE_COMPONENTS:
            return f"❌ 组件 '{key}' 不可编辑"

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
            label = COMPONENT_LABELS.get(key, key)
            return f"✅ 已更新全局模板: {label}\n💡 使用 {{{{agent}}}} 代表 agent 名字"

        agent = registry.get(agent_name)
        if not agent:
            return f"❌ Agent '{agent_name}' 不存在"

        if key == "persona":
            agent["prompt"] = content
            _persist_agent_file(agent_name, "SOUL.md", content, agents_dir or workspace)
        elif key == "examples":
            agent["examples"] = content
            _persist_agent_file(agent_name, "EXAMPLES.md", content, agents_dir or workspace)
        elif key == "instructions":
            agent["instructions"] = content
            _persist_agent_file(agent_name, "INSTRUCTIONS.md", content, agents_dir or workspace)
        elif key in ("main_prompt", "tool_instructions", "group_nudge"):
            overrides_file = Path.home() / ".nanobot" / "prompt_overrides.json"
            overrides: dict = {}
            if overrides_file.exists():
                try:
                    overrides = json.loads(overrides_file.read_text())
                except Exception:
                    pass
            overrides.setdefault(agent_name, {})[key] = content
            overrides_file.write_text(json.dumps(overrides, ensure_ascii=False, indent=2))
            agent[f"_override_{key}"] = content

        return f"✅ 已更新 {agent_name} 的 {COMPONENT_LABELS.get(key, key)}"


# ── Helpers ───────────────────────────────────────────────────

def _persist_agent_file(
    agent_name: str,
    filename: str,
    content: str,
    agents_dir: Path,
) -> None:
    """Write content to the agent's workspace file."""
    if not agents_dir.is_dir():
        logger.warning("Agents dir not found: {}", agents_dir)
        return
    for d in agents_dir.iterdir():
        if d.is_dir() and d.name.lower() == agent_name.lower():
            ws = d / "workspace"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / filename).write_text(content)
            logger.info("Persisted {} for agent {} ({} chars)", filename, agent_name, len(content))
            return
    logger.warning("Could not find agent dir for {}", agent_name)
