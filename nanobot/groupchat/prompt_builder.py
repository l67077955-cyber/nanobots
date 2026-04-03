"""prompt_builder.py — Agent 的 prompt/messages 构建器。

这个文件把 agent 的系统提示、对话历史、角色提示组装成 tool_loop 需要的 messages 列表。

核心方法：
    build_broadcast_prompt()  — 构建广播模式下 agent 的完整 messages
    build_leader_hint()       — 构建 leader 的控制指令文档（⚠️ 内容很重要）
    build_worker_hint()       — 构建普通 agent 的角色提示

数据流：
    broadcast.py → _build_runner() → prompt_builder.build_broadcast_prompt()
      → [system_prompt, history_messages, role_hint] → 传给 tool_loop

⚠️ agent 修改本文件时注意：
    1. build_leader_hint() 中的 control command 文档必须与 broadcast.py 的实际命令一致
    2. messages 格式必须是 [{"role": "system"|"user"|"assistant", "content": "..."}]
    3. 不要删除 state.yaml 的路径注入 — leader 需要知道文件路径才能 read_file
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
        "[Start a new group chat. Group members: {{members}}]\n"
        "[Current date and time: {{datetime}}]"
    ),
    "persona": "[从 SOUL.md 加载 — 在 /editagent 中编辑]",
    "tool_instructions": "",  # Loaded from ~/.nanobot/prompts/tool_instructions.md
    "broadcast_hint": (
        "[广播模式 — 多Agent协作]\n"
        "你是 {{agent_idx}}/{{total}} 号成员，代号 {{agent}}\n"
        "队友: {{teammates}}\n\n"
        "用户请求: {{user_question}}\n\n"
        "## 群聊工具\n"
        "- chatroom_send(to, message): 给队友发消息。to 可以是具体名字或 \"All\"\n\n"
        "### 协作通信协议\n"
        "chatroom_send(to=\"Harper\", message=\"搜索结果...\")\n"
        "chatroom_send(to=\"All\", message=\"关键发现...\")\n\n"
        "### 收到消息后的响应规则（关键！）\n"
        "收到队友消息时：执行请求 → 用 chatroom_send 回复结果。\n"
        "禁止：只在最终回复里提到，而不通过 chatroom_send 回复发送者。\n\n"
        "## 协作方式\n"
        "1. 先独立思考：分析问题 → 明确你能贡献什么 → 制定行动计划\n"
        "2. 执行工作（搜索/分析/编码），搜索后立即共享关键发现\n"
        "3. 用 chatroom_send 分享你的观点或发现（带来源 URL）\n"
        "4. 基于队友的信息补充分析，避免重复搜索已共享的内容\n"
        "5. 工作完成后直接在最终文字回复中给出结果\n\n"
        "## 搜索结果共享（关键！）\n"
        "- 你的搜索结果对队友也有价值，搜完后用 chatroom_send(to=\"All\") 分享\n"
        "- 收到队友的搜索结果后直接使用，不要重复搜索同样的内容\n"
        "- 需要补充时，换不同关键词或角度搜索\n\n"
        "## 限制\n"
        "- 必须先做工作再发言，不要空转\n"
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
            now = _cn_now().strftime("%Y年%m月%d日 %H:%M")
            return f"[Start a new group chat. Group members: {members}]\n[Current date and time: {now}]"
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
        """Build the memory system prompt (Claude Code memdir-inspired).

        Injects:
        1. MEMORY.md index content (always loaded, ≤200 lines)
        2. Memory file manifest (filename + description + type)
        3. Usage guide (how to read/write/search memories)
        4. Memory type taxonomy
        """
        try:
            from nanobot.agent.memory import MemoryStore
            store = MemoryStore(self._workspace)

            parts: list[str] = []
            parts.append("# 持久化记忆系统")
            parts.append("")
            parts.append(f"你有一个基于文件的记忆系统，位于 `{store.memory_dir}/`。")
            parts.append("记忆按主题拆分为独立文件，每个文件带 YAML frontmatter（name, description, type）。")
            parts.append("")

            # ── Section 1: Memory index (MEMORY.md content) ──
            index_content = store.read_long_term()
            memory_index = store.build_memory_index()

            if index_content and index_content.strip():
                parts.append("## MEMORY.md（长期索引）")
                parts.append("")
                # Truncate to prevent context explosion
                lines = index_content.strip().split("\n")
                if len(lines) > store.MAX_INDEX_LINES:
                    parts.append("\n".join(lines[:store.MAX_INDEX_LINES]))
                    parts.append(f"\n> ⚠️ 索引已截断（{len(lines)} 行，上限 {store.MAX_INDEX_LINES}）")
                else:
                    parts.append(index_content.strip())
                parts.append("")

            # ── Section 2: Memory file manifest ──
            if memory_index:
                parts.append("## 记忆文件清单")
                parts.append("")
                parts.append(memory_index)
                parts.append("")

            # ── Section 3: Usage guide ──
            parts.append("## 记忆操作")
            parts.append("")
            parts.append("### 读取记忆")
            parts.append(f"用 `read_file` 读取具体记忆文件（路径: `{store.memory_dir}/文件名.md`）")
            parts.append("")
            parts.append("### 搜索记忆")
            parts.append(f"用 `exec` 搜索: `grep -rn \"关键词\" {store.memory_dir}/ --include=\"*.md\"`")
            parts.append("")
            parts.append("### 保存新记忆")
            parts.append(f"用 `write_file` 创建新文件（路径: `{store.memory_dir}/文件名.md`），格式:")
            parts.append("```markdown")
            parts.append("---")
            parts.append("name: 记忆标题")
            parts.append("description: 一行描述（用于索引和检索）")
            parts.append("type: user|feedback|project|reference")
            parts.append("---")
            parts.append("")
            parts.append("具体内容...")
            parts.append("```")
            parts.append("")
            parts.append("### 记忆类型")
            parts.append("- **user** — 用户画像（角色、偏好、技能）")
            parts.append("- **feedback** — 行为反馈（纠正和确认）")
            parts.append("- **project** — 项目上下文（进度、Bug、决策）")
            parts.append("- **reference** — 外部引用（工具位置、链接）")
            parts.append("")
            parts.append("### 更新索引")
            parts.append(f"保存新记忆后，在 `{store.memory_file}` 中添加一行索引:")
            parts.append("`- [标题](文件名.md) — 一行描述`")
            parts.append("")

            # ── Section 4: Time log ──
            if store.history_file.exists():
                parts.append("### 时间线日志")
                parts.append(f"追加式日志: `{store.history_file}`")
                parts.append("")

            return "\n".join(parts)
        except Exception as e:
            logger.warning("Failed to build memory content: {}", e)
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
        context_exclude: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the full prompt messages list for an agent turn.

        Args:
            context_exclude: List of conversation seq numbers to hide from this agent.
                             Controlled by leader via state.yaml agents.X.context_exclude.
        """
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
                messages.extend(self.history_to_messages(
                    history, agent_name, context_exclude=context_exclude,
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
        context_exclude: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert history dicts into LLM API messages.

        Args:
            context_exclude: List of conversation seq numbers (1-indexed) to skip.
                             Leader controls this via state.yaml agents.X.context_exclude.
        """
        exclude_set = set(context_exclude or [])
        msgs: list[dict[str, Any]] = []
        for i, m in enumerate(history):
            seq = i + 1  # seq is 1-indexed
            if seq in exclude_set:
                continue  # Leader blocked this message for this agent
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

    # ── Broadcast prompt building ──

    def build_broadcast_prompt(
        self,
        agent_name: str,
        *,
        engine: Any,
        agents: list[str],
        user_question: str,
        leader_name: str | None = None,
        agent_idx: int = 0,
        total: int = 0,
    ) -> list[dict[str, Any]]:
        """Build the full prompt for an agent in broadcast mode.

        Reads context_exclude from state_bus to filter conversation history.
        """
        # Read context_exclude from state_bus
        context_exclude: list[int] | None = None
        if hasattr(engine, '_state_bus') and engine._state_bus:
            ctrl = engine._state_bus.get_agent_control(agent_name)
            context_exclude = ctrl.get("context_exclude") or None

        messages = engine._build_agent_prompt(agent_name, context_exclude=context_exclude)
        is_leader = (agent_name == leader_name)
        teammates = [a for a in agents if a != agent_name]

        overrides = self._load_prompt_overrides("__global__")

        if is_leader:
            hint = self.build_leader_hint(
                agent_name, agents, user_question,
                engine=engine,
            )
            messages.insert(max(len(messages) - 1, 0), {
                "role": "system",
                "content": hint,
            })
        else:
            # Standard broadcast hint
            hint_template = overrides.get("broadcast_hint") or self.get_component_template("broadcast_hint")
            if hint_template:
                hint = (
                    hint_template
                    .replace("{{agent_idx}}", str(agent_idx + 1))
                    .replace("{{total}}", str(total))
                    .replace("{{teammates}}", ", ".join(teammates))
                    .replace("{{agent}}", agent_name)
                    .replace("{{user_question}}", user_question)
                )
                messages.insert(max(len(messages) - 1, 0), {
                    "role": "system",
                    "content": hint,
                })

            # Leader-gated instructions for non-leader agents
            if leader_name:
                messages.insert(max(len(messages) - 1, 0), {
                    "role": "system",
                    "content": self.build_worker_hint(leader_name),
                })

        # Permission context for all agents
        perm_hint = self.build_permission_hint(
            agent_name, agents, engine=engine,
            leader_name=leader_name,
        )
        messages.insert(max(len(messages) - 1, 0), {
            "role": "system",
            "content": perm_hint,
        })

        return messages

    def build_leader_hint(
        self,
        leader_name: str,
        agents: list[str],
        user_question: str,
        *,
        engine: Any,
    ) -> str:
        """Build the leader agent's system prompt.

        Explains the pure variable-driven control via state.yaml.
        Leader directly modifies variables — no commands, no functions.
        """
        non_leader_agents = [a for a in agents if a != leader_name]
        agent_caps = []
        for a in non_leader_agents:
            a_cfg = engine.registry.get(a, {})
            a_tools = a_cfg.get("tools", {})
            if isinstance(a_tools, dict):
                on = [k for k, v in a_tools.items() if v]
            elif a_cfg.get("tools_enabled", False) or a_cfg.get("_default"):
                on = list(engine.TOOL_NAMES)
            else:
                on = []
            agent_caps.append(f"  {a}: {', '.join(on) if on else '(无工具)'}")

        # Get state.yaml path
        state_path = "~/.nanobot/collab-sessions/<session>/state.yaml"
        if hasattr(engine, '_state_bus') and engine._state_bus:
            state_path = str(engine._state_bus.path)
        elif hasattr(engine, '_session_dir') and engine._session_dir:
            state_path = str(engine._session_dir / "state.yaml")

        return (
            f"[Leader 模式 — 你是团队指挥官 👑]\n"
            f"你是 {leader_name}，负责分析问题、分配任务、整合结果。\n"
            f"只有你会自动启动，其他 agent 需要你通过修改 state.yaml 来唤起。\n\n"
            f"用户请求: {user_question}\n\n"
            f"## 团队成员及工具能力\n"
            + "\n".join(agent_caps) + "\n\n"
            f"## 控制面板 — 纯变量驱动\n"
            f"所有状态存储在: `{state_path}`\n"
            f"你通过 `read_file` 查看状态，通过 `edit_file` 修改变量来控制一切。\n\n"
            f"### 变量控制一览\n"
            f"| 你想做的事 | 怎么改 state.yaml |\n"
            f"|---|---|\n"
            f"| 启动 agent | 在 agents: 下新增一个 block，设 state: running |\n"
            f"| 暂停 agent | 改 agents.X.state: paused |\n"
            f"| 移除 agent | 删掉整个 agent block |\n"
            f"| 禁言 agent | 改 agents.X.muted: true |\n"
            f"| 屏蔽某条上下文 | 往 agents.X.context_exclude 加 seq 编号 |\n"
            f"| 控制回复对象 | 改 agents.X.reply_to (All/具体名/null) |\n"
            f"| 监视进度 | 读 agents.X.activity / current_tool / toolchain |\n"
            f"| 重排对话 | 直接编辑 conversation 数组 |\n"
            f"| 存自定义数据 | 写入 leader_data |\n"
            f"| **结束群聊** | **改 session.status: done** |\n\n"
            f"### agent block 模板\n"
            f"```yaml\n"
            f"agents:\n"
            f"  {non_leader_agents[0] if non_leader_agents else 'AgentName'}:\n"
            f"    state: running        # running | paused\n"
            f"    reply_to: All         # All | \"AgentName\" | null\n"
            f"    context_exclude: []   # 不让 agent 看到的 seq 编号\n"
            f"    muted: false\n"
            f"```\n\n"
            f"## 你的工具\n"
            f"- chatroom_send(to, message): 给队友发消息\n"
            f"- wait(timeout, from_agent): 等待队友回复\n"
            f"- read_file / edit_file: 查看和修改 state.yaml\n"
            f"- 基础工具（web_search 等）\n\n"
            f"## 工作流程\n"
            f"1. 分析问题，决定如何分工\n"
            f"2. edit_file state.yaml 新增 agent block 来唤起需要的 agent\n"
            f"   ⚠️ 只分配队友有工具能力完成的任务！\n"
            f"3. 自己也同步开展工作（搜索、分析）\n"
            f"4. read_file state.yaml 查看 agent 进度（activity/toolchain）\n"
            f"5. 信息充分后，在最终文字回复中整合所有发现给出完整答案\n"
        )


    @staticmethod
    def build_worker_hint(leader_name: str) -> str:
        """Build the worker agent's leader-following instructions."""
        return (
            f"[团队协作模式]\n"
            f"Leader {leader_name} 会通过 chatroom_send 给你分配任务。\n\n"
            f"━━ 工作流程 ━━\n"
            f"1. 收到任务后立即开展工作（搜索、分析、编码等）\n"
            f"2. 用 chatroom_send 向 Leader 汇报结果\n"
            f"3. 如果有后续任务会通过消息通知你\n"
            f"4. 完成所有工作后，在最终文字回复中给出完整结果\n\n"
            f"正确流程: 收到任务 → 做工作 → chatroom_send(结果) → 完成"
        )

    @staticmethod
    def build_permission_hint(
        agent_name: str,
        exec_agents: list[str],
        *,
        engine: Any,
        leader_name: str | None = None,
    ) -> str:
        """Build the tool permission context for an agent."""
        perm_lines = []
        for a in exec_agents:
            a_cfg = engine.registry.get(a, {})
            a_tools = a_cfg.get("tools", {})
            if isinstance(a_tools, dict):
                on = [k for k, v in a_tools.items() if v]
            elif a_cfg.get("tools_enabled", False) or a_cfg.get("_default"):
                on = list(engine.TOOL_NAMES)
            else:
                on = []
            extra = ""
            if a == agent_name:
                extra = " ← 你"
            elif a == leader_name:
                extra = " 👑Leader"
            perm_lines.append(f"  {a}: {', '.join(on) if on else '(无工具)'}{extra}")

        return (
            "[团队工具权限]\n"
            + "\n".join(perm_lines) + "\n\n"
            "注意：没有 web_search/web_fetch 权限时，也禁止用 exec 执行 curl/wget 等网络命令。\n"
            "如需搜索，请通过 chatroom_send 请求有搜索权限的队友帮忙。"
        )

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
