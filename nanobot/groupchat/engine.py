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
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.groupchat.agents import load_agents
from nanobot.groupchat.config import GroupChatConfig
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

        # Tool registry (same tools as core AgentLoop)
        # Lazy import to avoid circular: engine → agent.tools → agent → config → groupchat
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
        self.tools = ToolRegistry()
        self.tools.register(WebSearchTool(api_key=brave_api_key or None))
        self.tools.register(WebFetchTool())
        logger.info("Groupchat: registered {} tools: {}", len(self.tools), self.tools.tool_names)

        # Registry: all known agents {name: {model, prompt}}
        self.registry: dict[str, dict[str, Any]] = load_agents(config, workspace)

        # Active participants in current conversation
        self._active_agents: list[str] = []

        # Runtime state
        self._task: asyncio.Task | None = None
        self._running = False
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._send_fn: Callable[[str], Awaitable[None]] | None = None
        self._topic: str = ""
        self._history: list[dict[str, str]] = []
        self._request_log: list[dict[str, Any]] = []  # LLM request log
        self._session_dir: Path | None = None

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
        logger.info("Groupchat: added agent {}, active={}", matched, self._active_agents)

        # If we just hit 2+ agents and no group loop running, start it
        if len(self._active_agents) >= 2 and not self._running:
            self._start_group_loop()
            return (
                f"✅ {matched} 加入对话！\n"
                f"👥 当前成员: {', '.join(self._active_agents)}\n"
                f"🎭 群聊模式已启动"
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

    # ── Group Config Persistence ────────────────────────────

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
        """Load a saved group config, adding/removing agents as needed."""
        groups = self._load_groups()
        if name not in groups:
            available = ', '.join(groups.keys()) if groups else '无'
            return f"⚠️ 分组 「{name}」 不存在\n📋 可用分组: {available}"

        target = groups[name]
        # Remove agents not in target
        to_remove = [a for a in self._active_agents if a not in target]
        for a in to_remove:
            self.remove_agent(a)
        # Add agents in target
        added = []
        for a in target:
            if a not in self._active_agents:
                result = self.add_agent(a)
                if "加入" in result:
                    added.append(a)

        return (
            f"✅ 已载入分组 「{name}」\n"
            f"👥 当前成员: {', '.join(self._active_agents)}"
        )

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
            return "📋 没有保存的分组\n用 /savegroup <名称> 保存当前成员"
        lines = ["📋 已保存的分组："]
        for gname, members in groups.items():
            lines.append(f"  • {gname}: {', '.join(members)}")
        return "\n".join(lines)

    # ── Tool-augmented chat (matching AgentLoop._run_agent_loop) ───

    async def _chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        model: str,
        agent_name: str,
        max_iterations: int = 10,
    ) -> tuple[str, list[str]]:
        """Chat with tool calling loop, matching nanobot core agent logic.

        Returns (content, tools_used).
        """
        tool_defs = self.tools.get_definitions()
        tools_used: list[str] = []
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=tool_defs if tool_defs else None,
                model=model,
                max_tokens=self.config.max_tokens,
            )

            if response.has_tool_calls:
                # Show tool usage hint
                hints = ", ".join(tc.name for tc in response.tool_calls)
                logger.info("Agent {} calling tools: {}", agent_name, hints)
                if self._send_fn:
                    await self._send(f"🔧 {agent_name} 正在使用: {hints}")

                # Append assistant message with tool_calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages.append({
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": tool_call_dicts,
                })

                # Execute each tool and append results
                for tc in response.tool_calls:
                    tools_used.append(tc.name)
                    logger.info("Executing tool: {}({})", tc.name,
                                json.dumps(tc.arguments, ensure_ascii=False)[:200])
                    result = await self.tools.execute(tc.name, tc.arguments)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result[:2000],  # Truncate large results
                    })
            else:
                # Text response — done
                content = response.content or ""
                # Strip agent's own name prefix if model mimics history format
                # e.g. "Benjamin: actual reply" → "actual reply"
                for prefix in (f"{agent_name}: ", f"{agent_name}："):
                    if content.startswith(prefix):
                        content = content[len(prefix):]
                        break
                return content, tools_used

        # Max iterations reached
        logger.warning("Agent {} hit max tool iterations ({})", agent_name, max_iterations)
        content = response.content or ""
        for prefix in (f"{agent_name}: ", f"{agent_name}："):
            if content.startswith(prefix):
                content = content[len(prefix):]
                break
        return content, tools_used

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
        messages: list[dict[str, str]] = [
            {"role": "system", "content": agent["prompt"]},
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

        try:
            content, tools_used = await self._chat_with_tools(
                messages=messages,
                model=agent["model"],
                agent_name=agent_name,
            )
            self._request_log.append({
                "agent": agent_name, "model": agent["model"],
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": len(content), "time": datetime.now().strftime("%H:%M:%S"),
                "mode": "direct", "tools": tools_used,
            })
            if content:
                self._add_message("用户", user_message)
                self._add_message(agent_name, content)
            return f"💬 {agent_name}:\n\n{content}" if content else None
        except Exception as e:
            logger.error("Direct chat with {} failed: {}", agent_name, e)
            self._request_log.append({
                "agent": agent_name, "model": agent["model"],
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": 0, "time": datetime.now().strftime("%H:%M:%S"),
                "mode": "direct", "error": str(e),
            })
            return f"⚠️ {agent_name} 回复失败: {e}"

    def inject(self, message: str) -> None:
        """Inject a user message into the group chat."""
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

    # ── Internal ─────────────────────────────────────────────

    def _resolve_agent_name(self, name: str) -> str | None:
        """Case-insensitive agent name resolution."""
        for reg_name in self.registry:
            if reg_name.lower() == name.lower():
                return reg_name
        return None

    def _start_group_loop(self) -> None:
        """Start the async group chat loop."""
        if self._running:
            return
        self._running = True
        self._input_queue = asyncio.Queue()
        if not self._topic:
            self._topic = "自由讨论"

        # Session directory
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
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

    def _history_to_messages(self, current_agent: str = "") -> list[dict[str, str]]:
        """Convert history into proper API messages matching SillyTavern format.

        SillyTavern group chat format:
        - User messages → role:user
        - ALL agent messages → role:assistant with "AgentName: " prefix
        """
        msgs: list[dict[str, str]] = []
        for m in self._history:
            sender = m["sender"]
            content = m["content"]
            if sender == "用户":
                msgs.append({"role": "user", "content": content})
            elif sender == "系统":
                msgs.append({"role": "system", "content": content})
            else:
                # All agent messages as assistant with name prefix (SillyTavern style)
                msgs.append({"role": "assistant", "content": f"{sender}: {content}"})
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

    def _build_agent_prompt(self, agent_name: str) -> list[dict[str, str]]:
        """Build prompt matching SillyTavern's group chat API format.

        SillyTavern sends:
        1. system: agent persona
        2. user: original question
        3. assistant: "PreviousAgent: response..." (previous replies)
        Minimal system noise — let the model focus on reasoning.
        """
        agent = self.registry[agent_name]

        messages: list[dict[str, str]] = []

        # 1. Persona (system)
        messages.append({"role": "system", "content": agent["prompt"]})

        # 2. Few-shot examples (only if EXAMPLES.md exists)
        examples = agent.get("examples", "")
        if examples:
            messages.append({"role": "system", "content": f"以下是你的对话风格示例：\n{examples}"})

        # 3. Chat history as proper multi-message (SillyTavern format)
        messages.extend(self._history_to_messages(agent_name))

        # 4. If no user message in history yet, that means we need a trigger
        # Check if last message is from user or we need to add a nudge
        if not self._history or self._history[-1]["sender"] != "用户":
            # All messages so far are from agents — add a group nudge
            messages.append({"role": "system", "content": f"[Write the next reply only as {agent_name}.]"})

        return messages

    async def _agent_speak(self, agent_name: str) -> None:
        if agent_name not in self.registry:
            return
        model = self.registry[agent_name]["model"]
        messages = self._build_agent_prompt(agent_name)

        try:
            content, tools_used = await self._chat_with_tools(
                messages=messages,
                model=model,
                agent_name=agent_name,
            )
            self._request_log.append({
                "agent": agent_name, "model": model,
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": len(content), "time": datetime.now().strftime("%H:%M:%S"),
                "mode": "group", "tools": tools_used,
            })
            if content:
                self._add_message(agent_name, content)
                await self._send(f"💬 {agent_name}:\n\n{content}")
        except Exception as e:
            logger.error("Groupchat: {} LLM call failed: {}", agent_name, e)
            self._request_log.append({
                "agent": agent_name, "model": model,
                "msgs": len(messages), "max_tokens": self.config.max_tokens,
                "reply_len": 0, "time": datetime.now().strftime("%H:%M:%S"),
                "mode": "group", "error": str(e),
            })
            await self._send(f"⚠️ {agent_name} 回复失败: {e}")

    async def _generate_summary(self) -> None:
        if not self._history:
            return
        # Use first active agent's model
        agent_name = self._active_agents[0] if self._active_agents else list(self.registry.keys())[0]
        model = self.registry[agent_name]["model"]

        try:
            response = await self.provider.chat(
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

                # All agents speak one round
                current_agents = list(self._active_agents)
                for name in current_agents:
                    if not self._running or name not in self._active_agents:
                        break
                    await asyncio.sleep(self.config.auto_reply_delay)
                    await self._agent_speak(name)

            if self._running:
                await self._send("🔚 群聊结束！正在生成总结...")
                await self._generate_summary()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Group chat loop error: {}", e)
            await self._send(f"❌ 群聊异常: {e}")
        finally:
            self._running = False
            logger.info("Group chat loop ended")
