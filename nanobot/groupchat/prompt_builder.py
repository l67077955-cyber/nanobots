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
    "main_prompt", "group_context", "persona", "memory",
    "tool_instructions", "skills", "broadcast_hint", "examples",
    "history", "instructions", "leader_prompt", "group_nudge",
]

COMPONENT_LABELS: dict[str, str] = {
    "main_prompt": "主提示 (main_prompt)",
    "group_context": "群聊上下文 (group_context)",
    "persona": "人设/SOUL (persona)",
    "memory": "长期记忆 (memory)",
    "tool_instructions": "工具指令 (tool_instructions)",
    "skills": "技能列表 (skills)",
    "broadcast_hint": "广播协调 (broadcast_hint)",
    "examples": "示例对话 (examples)",
    "history": "聊天记录 (history)",
    "instructions": "后置指令 (instructions)",
    "leader_prompt": "领袖指令 (leader_prompt)",
    "group_nudge": "群聊规范 (group_nudge)",
}

GLOBAL_EDITABLE: set[str] = {
    "main_prompt", "group_context", "tool_instructions", "skills", "memory",
    "broadcast_hint", "examples", "instructions", "leader_prompt", "group_nudge",
}
AGENT_EDITABLE = {"persona"}
EDITABLE_COMPONENTS = GLOBAL_EDITABLE | AGENT_EDITABLE


def _load_custom_labels() -> dict[str, str]:
    """Load user-defined custom component labels from disk."""
    f = Path.home() / ".nanobot" / "custom_prompt_labels.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _save_custom_labels(labels: dict[str, str]) -> None:
    """Persist custom component labels to disk."""
    f = Path.home() / ".nanobot" / "custom_prompt_labels.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(labels, ensure_ascii=False, indent=2))


def _register_custom_labels() -> None:
    """Load custom labels from disk and merge into module-level dicts."""
    for key, label in _load_custom_labels().items():
        COMPONENT_LABELS[key] = label
        GLOBAL_EDITABLE.add(key)


# Auto-register on import so custom components survive restarts
_register_custom_labels()


# ── Template Defaults ─────────────────────────────────────────

TEMPLATES: dict[str, str] = {
    "main_prompt": (
        "Write {{agent}}'s next reply in a group chat. "
        "Write 1 reply only in character as {{agent}}. "
        "Do not write as or for other characters. "
        "Focus on executing the user's request — do not just greet or ask what to do."
    ),
    "group_context": (
        "[Start a new group chat. Group members: {{members}}]"
    ),
    "persona": "[从 SOUL.md 加载 — 在 /editagent 中编辑]",
    "tool_instructions": "",  # Loaded from ~/.nanobot/prompts/tool_instructions.md
    "broadcast_hint": (
        "[广播模式 — 多Agent协作]\n"
        "你是 {{agent_idx}}/{{total}} 号成员，代号 {{agent}}\n"
        "队友: {{teammates}}\n\n"
        "用户请求: {{user_question}}\n\n"
        "## 聊天记录说明\n"
        "历史记录仅包含：你自己的发言、用户消息、系统消息。\n"
        "队友的历史发言不在历史里——他们本轮的消息通过 chatroom_send/wait 实时传达。\n"
        "若历史中出现 [早期对话摘要]，表示早期消息已被压缩为摘要；\n"
        "若出现 [...N 条历史消息已省略...]，表示受上下文限制部分记录不可见，以最近内容为准。\n\n"
        "## 群聊工具\n"
        "- chatroom_send(to, message): 给队友发消息。to 可以是具体名字或 \"All\"\n"
        "- wait(timeout=30): 等待队友消息，不要超过60s\n\n"
        "### 协作通信协议\n"
        "chatroom_send(to=\"Harper\", message=\"搜索结果...\")\n"
        "chatroom_send(to=\"All\", message=\"关键发现...\")\n\n"
        "### 收到消息后的响应规则（关键！）\n"
        "收到队友消息时：执行请求 → 用 chatroom_send 回复结果。\n"
        "禁止：只在最终回复里提到，而不通过 chatroom_send 回复发送者。\n\n"
        "## 发言顺序\n"
        "系统使用对话资源池控制消息量：每条消息消耗槽位（发All=3，发个人=1）。\n"
        "池满时 chatroom_send 会阻塞，直到有人 wait() 释放槽位。\n\n"
        "## 协作方式\n"
        "1. 先独立思考：分析问题 → 明确你能贡献什么 → 制定行动计划\n"
        "2. 执行工作（搜索/分析/编码），搜索后立即共享关键发现\n"
        "3. 用 chatroom_send 分享你的观点或发现（带来源 URL）\n"
        "4. 用 wait() 听队友的消息\n"
        "5. 基于队友的信息补充分析，避免重复搜索已共享的内容\n"
        "6. 当所有人都在 wait 时，系统会自动结束本轮讨论\n\n"
        "## 搜索结果共享（关键！）\n"
        "- 你的搜索结果对队友也有价值，搜完后用 chatroom_send(to=\"All\") 分享\n"
        "- 收到队友的搜索结果后直接使用，不要重复搜索同样的内容\n"
        "- 需要补充时，换不同关键词或角度搜索\n\n"
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

    @staticmethod
    def add_custom_component(key: str, label: str) -> str:
        """Register a user-defined custom component.

        Adds it to COMPONENT_LABELS, GLOBAL_EDITABLE, and persists the label.
        Does NOT add to the prompt order — caller should do that separately.
        """
        if key in COMPONENT_LABELS:
            return f"❌ 组件 '{key}' 已存在"
        # Register in module-level dicts
        COMPONENT_LABELS[key] = label
        GLOBAL_EDITABLE.add(key)
        # Persist
        custom = _load_custom_labels()
        custom[key] = label
        _save_custom_labels(custom)
        return f"✅ 已创建自定义组件: {label}"

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
                "editable": key in GLOBAL_EDITABLE or key in AGENT_EDITABLE,
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
            return f"[Start a new group chat. Group members: {members}]"
        elif key == "persona":
            return agent.get("prompt", "")
        elif key == "memory":
            return self._build_memory_content()
        elif key == "tool_instructions":
            return self.get_component_template("tool_instructions")
        elif key == "skills":
            return self._build_skills_content()
        elif key == "examples":
            return agent.get("examples", "")
        elif key == "history":
            return "[聊天记录 — 动态插入]"
        elif key == "instructions":
            return agent.get("instructions", "")
        elif key == "leader_prompt":
            if leader == agent_name:
                return self.get_component_template("leader_prompt") or "[Leader prompt — 自动生成]"
            return ""
        elif key in ("broadcast_hint", "group_nudge"):
            return self.get_component_template(key)
        # Custom components or others: check file
        return self.get_component_template(key)

    def _build_skills_content(self) -> str:
        """Build the skills section for group chat agents.

        Uses SkillsLoader to generate an XML summary of available skills.
        Agents can read the full SKILL.md via read_file for progressive loading.
        """
        from nanobot.agent.skills import SkillsLoader

        loader = SkillsLoader(self._workspace)
        summary = loader.build_skills_summary()
        if not summary:
            return ""
        return (
            "[Skills — 可用技能列表]\n\n"
            "以下技能扩展你的能力。需要时用 read_file 读取对应的 SKILL.md 来使用。\n"
            "标记 available=\"false\" 的技能需要先安装依赖。\n\n"
            + summary
        )

    def _build_memory_content(self) -> str:
        """Build the long-term memory hint for progressive loading.

        Instead of injecting full MEMORY.md content (which grows over time),
        provide a brief pointer so the agent can read_file when relevant.
        Matches the progressive-loading pattern used by skills.
        """
        try:
            from nanobot.agent.memory import MemoryStore
            store = MemoryStore(self._workspace)
            content = store.read_long_term()
            if content and content.strip():
                # Show first non-empty line as preview
                preview = ""
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        preview = stripped[:80]
                        break
                mem_path = store.memory_file
                history_path = store.history_file
                hint = (
                    "[Long-term Memory — 长期记忆]\n\n"
                    f"你有持久化记忆文件。用 read_file 查看完整内容：\n"
                    f"- `{mem_path}` — 长期事实记忆 (MEMORY.md)\n"
                    f"- `{history_path}` — 时间线日志 (HISTORY.md)\n"
                )
                if preview:
                    hint += f"\n预览: {preview}…"
                return hint
        except Exception as e:
            logger.warning("Failed to load memory hint: {}", e)
        return ""

    @staticmethod
    def get_component_template(key: str) -> str:
        content = TEMPLATES.get(key, "")
        # For any component, check ~/.nanobot/prompts/{key}.md as file-based override
        if not content:
            f = Path.home() / ".nanobot" / "prompts" / f"{key}.md"
            if f.exists():
                try:
                    content = f.read_text().strip()
                except Exception:
                    pass
        return content

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
        relevant_agents: list[str] | None = None,
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
                from nanobot.groupchat.history_settings import max_context_chars
                messages.extend(self.history_to_messages(
                    history, agent_name,
                    max_chars=max_context_chars(),
                    relevant_agents=relevant_agents,
                ))
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
        max_chars: int = 0,
        pin_first_user: bool = True,
        relevant_agents: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert history dicts into LLM API messages.

        When max_chars > 0, applies a budget strategy:
        - Pins the first user message (preserves original intent)
        - Fills remaining budget from the tail (most recent messages)
        - Inserts a system placeholder if any middle messages were skipped

        When relevant_agents is set, other agents' messages are filtered out
        (用户 and 系统 messages are always kept). Used in broadcast mode so
        each agent only sees its own prior turns rather than every agent's output.
        """
        def _to_msg(m: dict[str, str]) -> dict[str, Any]:
            sender, content = m["sender"], m["content"]
            if sender == "用户":
                return {"role": "user", "content": content}
            elif sender == "系统":
                return {"role": "system", "content": content}
            else:
                return {
                    "role": "assistant",
                    "content": f"{sender}: {content}",
                    "name": sender.replace(" ", "_"),
                }

        # Apply agent filter before any budget logic
        filtered = history
        if relevant_agents is not None:
            filtered = [
                m for m in history
                if m["sender"] in ("用户", "系统") or m["sender"] in relevant_agents
            ]

        msgs_full = [_to_msg(m) for m in filtered]

        if not max_chars or not msgs_full:
            return msgs_full

        # Find first user message to pin
        pinned: list[dict[str, Any]] = []
        rest_start = 0
        if pin_first_user:
            for i, m in enumerate(msgs_full):
                if m["role"] == "user":
                    pinned = [m]
                    rest_start = i + 1
                    break

        # Fill from tail within remaining budget
        pinned_chars = sum(len(m.get("content", "")) for m in pinned)
        budget = max_chars - pinned_chars
        tail: list[dict[str, Any]] = []
        for m in reversed(msgs_full[rest_start:]):
            c = len(m.get("content", ""))
            if budget - c < 0:
                break
            tail.insert(0, m)
            budget -= c

        skipped = len(msgs_full) - rest_start - len(tail)
        result = list(pinned)
        if skipped > 0:
            result.append({
                "role": "system",
                "content": f"[...{skipped} 条历史消息已省略以节省上下文...]",
            })
        result.extend(tail)
        return result

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
        if key not in GLOBAL_EDITABLE and key not in AGENT_EDITABLE:
            return f"❌ 组件 '{key}' 不可编辑"

        if agent_name == "__global__":
            # Save to ~/.nanobot/prompts/{key}.md
            prompts_dir = Path.home() / ".nanobot" / "prompts"
            prompts_dir.mkdir(parents=True, exist_ok=True)
            md_file = prompts_dir / f"{key}.md"
            md_file.write_text(content)
            # Also update prompt_overrides.json for backward compatibility
            overrides_file = Path.home() / ".nanobot" / "prompt_overrides.json"
            overrides: dict = {}
            if overrides_file.exists():
                try:
                    overrides = json.loads(overrides_file.read_text())
                except Exception:
                    pass
            overrides.setdefault("__global__", {})[key] = content
            overrides_file.write_text(json.dumps(overrides, ensure_ascii=False, indent=2))
            label = COMPONENT_LABELS.get(key, key)
            return f"✅ 已更新全局模板: {label}\n📄 已保存到 prompts/{key}.md\n💡 使用 {{{{agent}}}} 代表 agent 名字"

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
