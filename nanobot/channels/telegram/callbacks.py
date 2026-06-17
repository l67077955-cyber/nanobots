"""Inline keyboard callback dispatcher for Telegram."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.history.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
    COMPONENT_PHASES as _COMPONENT_PHASES,
)
from .formatting import TELEGRAM_MAX_MESSAGE_LEN, to_cli_style


class CallbacksMixin:
    """Mixin providing inline keyboard callback handling."""

    @staticmethod
    def _sort_models_newest_first(model_ids: list[str]) -> list[str]:
        """Sort model IDs newest-to-oldest by extracting YYYYMMDD dates; reverse-alphabetical fallback."""
        def _key(mid: str) -> tuple:
            m = re.search(r'(20\d{6})', mid)
            return (int(m.group(1)) if m else 0, mid)
        return sorted(model_ids, key=_key, reverse=True)

    @staticmethod
    def _build_model_buttons_2col(
        model_ids: list[str],
        prov: str,
        existing: set[str],
        strip_prefix: str | None = None,
    ) -> list[list]:
        """Build 2-column inline keyboard buttons for a model list.

        Already-added models are listed in text only (no button).
        strip_prefix: if given, remove 'prefix/' from display label.
        """
        buttons: list[list] = []
        row: list = []
        for mid in model_ids:
            if mid in existing:
                continue  # shown in text, no button
            cb = f"ep_addm:{prov}:{mid}"
            if len(cb.encode()) > 64:
                continue
            label = mid[len(strip_prefix) + 1:] if strip_prefix and mid.startswith(f"{strip_prefix}/") else mid
            row.append(InlineKeyboardButton(f"+ {label}", callback_data=cb))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return buttons


    async def _send_agent_hyperparams_keyboard(self, chat_id: str, agent_name: str, agent_hp) -> None:
        if not isinstance(agent_hp, dict):
            agent_hp = {}
        """Send per-agent hyperparams keyboard."""
        buttons = []
        if agent_hp:
            for k, v in agent_hp.items():
                buttons.append([InlineKeyboardButton(f"✏️ {k} = {v}", callback_data=f"ahp:{agent_name}:{k}"),
                                InlineKeyboardButton("🗑️", callback_data=f"ahp_del:{agent_name}:{k}")])
        else:
            buttons.append([InlineKeyboardButton("（无参数）", callback_data="noop")])
        buttons.append([
            InlineKeyboardButton("➕ 添加参数", callback_data=f"ahp_add:{agent_name}"),
            InlineKeyboardButton("📥 复制全局设置", callback_data=f"ahp_sync:{agent_name}")
        ])
        buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{agent_name}")])
        text = (
            f"⚙️ {agent_name} 高级超参数（可选）\n\n"
            "这些是底层采样参数（temperature、top_p 等）。\n"
            "大多数用户只需用上方的「思考深度」即可获得想要的效果。\n"
            "除非你清楚知道每个参数的作用，否则建议保持为空（继承全局或模型默认）。"
        )
        if agent_hp:
            text += f"\n\n当前覆盖值：\n" + "\n".join(f"  {k} = {v}" for k, v in agent_hp.items())
        else:
            text += "\n\n（当前无覆盖，使用全局/默认）"
        await self._app.bot.send_message(
            chat_id=int(chat_id), text=text[:4096],
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle InlineKeyboard button presses."""
        query = update.callback_query
        if not query or not query.data:
            return
        logger.debug("Callback received: data={} from={}", query.data, query.from_user.id if query.from_user else "?")
        await query.answer()

        try:
            data = query.data
            chat_id = str(query.message.chat_id)

            if data.startswith("add:"):
                name = data[4:]
                self._ensure_gc_send(chat_id)
                result = self._groupchat_engine.add_agent(name)
                await query.edit_message_text(result)

            elif data.startswith("rm:"):
                name = data[3:]
                result = self._groupchat_engine.remove_agent(name)
                await query.edit_message_text(result)

            elif data.startswith("edit:"):
                name = data[5:]
                agent = self._groupchat_engine.registry.get(name)
                if not agent:
                    await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                    return
                await query.edit_message_text(
                    self._edit_menu_text(name),
                    reply_markup=self._edit_menu_buttons(name),
                )

            elif data.startswith("da:"):
                # da:AgentName — show delete confirmation
                name = data[3:]
                agent = self._groupchat_engine.registry.get(name)
                if not agent:
                    await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                    return
                await query.edit_message_text(
                    f"🗑️ 删除 Agent: {name}\n\n"
                    f"模型: {agent.get('model', '?')}\n\n"
                    "⚠️ 此操作将永久删除该 agent 的配置文件，无法恢复！\n"
                    "确认删除吗？",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ 确认删除", callback_data=f"dac:{name}:yes")],
                        [InlineKeyboardButton("❌ 取消", callback_data=f"edit:{name}")],
                    ]),
                )

            elif data.startswith("dac:"):
                # dac:AgentName:yes/no — confirm or cancel delete
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                name, confirm = parts[1], parts[2]
                if confirm != "yes":
                    await query.edit_message_text("❌ 已取消")
                    return
                engine = self._groupchat_engine
                if not engine or name not in engine.registry:
                    await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                    return
            
                deleted_dir = engine.delete_agent(name)
            
                msg = f"🗑️ Agent '{name}' 已删除"
                if deleted_dir:
                    msg += f"\n📁 配置目录已删除"
                await query.edit_message_text(msg)

            elif data.startswith("ef:"):
                # ef:AgentName:field
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                name, field = parts[1], parts[2]
                if field == "cancel":
                    self._edit_state.pop(chat_id, None)
                    await query.edit_message_text("❌ 已取消")
                    return
                if field == "tools":
                    # Show per-tool toggle buttons
                    from nanobot.groupchat.orchestra.engine import GroupChatEngine
                    agent = self._groupchat_engine.registry.get(name, {})
                    tools_cfg = agent.get("tools")
                    # Migrate legacy tools_enabled to granular dict
                    if not isinstance(tools_cfg, dict) or "web_search" not in tools_cfg:
                        all_on = agent.get("tools_enabled", False)
                        tools_cfg = {t: all_on for t in GroupChatEngine.TOOL_NAMES}
                        agent["tools"] = tools_cfg

                    labels = {
                        "web_search": "🔍 网页搜索",
                        "web_fetch": "🌐 网页抓取",
                        "exec": "⚡ 执行命令",
                        "read_file": "📄 读文件",
                        "write_file": "✍️ 写文件",
                        "edit_file": "✂️ 编辑文件",
                        "list_dir": "📁 列目录",
                        "memory_palace": "🧠 记忆宫殿",
                    }
                    buttons = []
                    for t in GroupChatEngine.TOOL_NAMES:
                        on = tools_cfg.get(t, False)
                        icon = "✅" if on else "❌"
                        label = labels.get(t, t)
                        buttons.append([InlineKeyboardButton(
                            f"{icon} {label}",
                            callback_data=f"tf:{name}:{t}"
                        )])
                    buttons.append([InlineKeyboardButton("✅ 全开", callback_data=f"tf:{name}:__all_on"),
                                    InlineKeyboardButton("❌ 全关", callback_data=f"tf:{name}:__all_off")])
                    buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])
                    await query.edit_message_text(
                        f"🔧 {name} 工具权限设置:",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return
                elif field == "rank":
                    from nanobot.groupchat.display.visibility import RANK_DISPLAY, RANK_ORDER, resolve_rank

                    agent = self._groupchat_engine.registry.get(name, {})
                    current = agent.get("rank")
                    MODERN_RANKS = list(RANK_ORDER.keys())
                    resolved = resolve_rank(current, agent=name) if current is not None else "basic"
                    if resolved:
                        current_label = RANK_DISPLAY[resolved]
                    elif current is not None:
                        current_label = f"无效: {current}"
                    else:
                        current_label = RANK_DISPLAY["basic"]
                    selected = resolved if resolved else (current if current in MODERN_RANKS else None)
                    buttons = []
                    for r in MODERN_RANKS:
                        icon = "✅ " if r == selected else "  "
                        buttons.append([InlineKeyboardButton(
                            f"{icon}{RANK_DISPLAY[r]}", callback_data=f"srr:{name}:{r}"
                        )])
                    buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])
                    await query.edit_message_text(
                        f"🎖️ {name} 等级设置 (当前: {current_label})\n\n"
                        f"更改 rank 会立即更新中断权限；对话池/搜索额度在本轮内不变，新轮次生效。",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return
                elif field == "hyperparams":
                    # Per-agent hyperparams (same UX as /hyperparams but per-agent)
                    agent = self._groupchat_engine.registry.get(name, {})
                    agent_hp = agent.get("hyperparams") or {}
                    await self._send_agent_hyperparams_keyboard(chat_id, name, agent_hp)
                    return
                elif field == "reasoning_effort":
                    # Show effort level selection — friendly "思考深度"
                    agent = self._groupchat_engine.registry.get(name, {})
                    current = agent.get("reasoning_effort") or "off"
                    levels = [("off", "默认(自动)"), ("low", "低"), ("medium", "中"), ("high", "高")]
                    buttons = []
                    for lvl, lbl in levels:
                        icon = "✅" if lvl == current else "⭕"
                        buttons.append([InlineKeyboardButton(
                            f"{icon} {lbl} ({lvl})",
                            callback_data=f"ef_re:{name}:{lvl}"
                        )])
                    buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])
                    help_text = (
                        "🧠 思考深度（reasoning_effort）\n\n"
                        "• 默认：让模型自己决定\n"
                        "• 低：快速响应，适合简单任务\n"
                        "• 中：平衡质量与速度（推荐多数场景）\n"
                        "• 高：模型会进行更深入的内部推理（仅支持 o1 / claude-thinking 等模型，耗时更长、token 更多，但思考更透彻）\n\n"
                        "新手建议从「中」或「默认」开始。"
                    )
                    await query.edit_message_text(
                        f"🧠 {name} 思考深度 (当前: {current})\n\n{help_text}",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return
                elif field == "presets":
                    # Simple high-level presets that hide complexity
                    buttons = [
                        [InlineKeyboardButton("⚖️ 平衡（推荐）", callback_data=f"preset:{name}:balanced")],
                        [InlineKeyboardButton("✨ 更有创意", callback_data=f"preset:{name}:creative")],
                        [InlineKeyboardButton("🔬 更严谨分析", callback_data=f"preset:{name}:precise")],
                        [InlineKeyboardButton("🧠 深度思考", callback_data=f"preset:{name}:deep")],
                        [InlineKeyboardButton("↩️ 恢复默认", callback_data=f"preset:{name}:reset")],
                        [InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")],
                    ]
                    await query.edit_message_text(
                        f"🎯 {name} 快速预设\n\n"
                        "这些一键设置会同时调整思考深度和少量采样参数，适合不想碰底层超参数的用户。\n"
                        "• 平衡：默认或中强度\n"
                        "• 更有创意：较高随机性 + 中/高思考\n"
                        "• 更严谨分析：低温度 + 中强度\n"
                        "• 深度思考：高思考强度（适合支持的模型）\n"
                        "• 恢复默认：清除本 agent 的高级覆盖",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return
                self._edit_state[chat_id] = {"agent": name, "field": field}
                if field == "persona":
                    current = self._groupchat_engine.registry.get(name, {}).get("prompt", "")
                    await query.edit_message_text(f"📄 当前人设:\n\n{current[:3000]}")
                    await self._gc_send(chat_id, "请输入新人设内容:")
                elif field == "model":
                    # Show provider selection keyboard
                    pm = self._load_pm()
                    provs = list(pm.get("providers", {}).keys())
                    if provs:
                        buttons = [[InlineKeyboardButton(f"🏢 {p}", callback_data=f"em_prov:{name}:{p}")] for p in provs]
                        buttons.append([InlineKeyboardButton("✏️ 手动输入", callback_data=f"em_manual:{name}")])
                        await query.edit_message_text("🤖 选择提供商:", reply_markup=InlineKeyboardMarkup(buttons))
                    else:
                        await query.edit_message_text("请输入新模型名 (如 anthropic/claude-sonnet-4-5):")
                else:
                    prompts = {"name": "新名字"}
                    await query.edit_message_text(f"请输入{prompts.get(field, field)}:")

            elif data.startswith("log:"):
                mode = data[4:]
                engine = self._groupchat_engine
                if not engine or (not engine.history.messages and not engine.request_log):
                    await query.edit_message_text("📭 无日志")
                    return
                rlog = engine.request_log
                history = engine.history.messages
                if mode == "brief":
                    # Brief: last 5 requests
                    entries = rlog[-5:] if rlog else []
                    lines = [f"📋 最近请求 ({len(entries)}/{len(rlog)}):\n"]
                    for r in entries:
                        err = " ❌" if r.get("error") else ""
                        lines.append(f"[{r['time']}] {r['agent']} → {r['model']} | msgs:{r['msgs']} reply:{r['reply_len']}字{err}")
                    if history:
                        lines.append(f"\n💬 对话: {len(history)} 条")
                    await query.edit_message_text("\n".join(lines))
                else:
                    # Full: all requests + chat
                    lines = [f"📜 完整日志 ({len(rlog)} 请求, {len(history)} 对话):\n"]
                    lines.append("── 请求记录 ──")
                    for i, r in enumerate(rlog, 1):
                        err = f" | ❌ {r['error'][:50]}" if r.get("error") else ""
                        lines.append(f"{i}. [{r['time']}] {r['mode']} | {r['agent']} → {r['model']} | msgs:{r['msgs']} max:{r['max_tokens']} reply:{r['reply_len']}字{err}")
                    lines.append("\n── 对话记录 ──")
                    for m in history[-10:]:
                        text = m['content'][:100] + "..." if len(m['content']) > 100 else m['content']
                        lines.append(f"[{m['sender']}]: {text}")
                    full = "\n".join(lines)
                    await query.edit_message_text(full[:4096])

            elif data.startswith("ef_re:"):
                # ef_re:AgentName:level — set reasoning effort
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                name, lvl = parts[1], parts[2]
                engine = self._groupchat_engine
                if not engine or name not in engine.registry:
                    await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                    return

                effort: str | None = None if lvl == "off" else lvl
                cfg = engine.registry[name]
                cfg["reasoning_effort"] = effort

                # Persist to disk
                cfg_path = Path.home() / ".nanobot" / "agents" / name.lower() / "config.json"
                if cfg_path.exists():
                    try:
                        file_cfg = json.loads(cfg_path.read_text())
                        file_cfg["reasoning_effort"] = effort
                        cfg_path.write_text(json.dumps(file_cfg, indent=2, ensure_ascii=False))
                    except Exception:
                        pass

                # Refresh the menu
                current = effort or "off"
                levels = [("off", "默认(自动)"), ("low", "低"), ("medium", "中"), ("high", "高")]
                buttons = []
                for l_lvl, lbl in levels:
                    icon = "✅" if l_lvl == current else "⭕"
                    buttons.append([InlineKeyboardButton(
                        f"{icon} {lbl} ({l_lvl})",
                        callback_data=f"ef_re:{name}:{l_lvl}"
                    )])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])
                await query.edit_message_text(
                    f"🧠 {name} 思考深度 (当前: {current})\n\n已更新。支持的模型会据此进行不同深度的推理。",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif data.startswith("preset:"):
                # One-click presets for common user desires (hides raw hyperparams complexity)
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                name, preset = parts[1], parts[2]
                agent = self._groupchat_engine.registry.get(name, {}) if self._groupchat_engine else {}
                cfg_path = Path.home() / ".nanobot" / "agents" / name.lower() / "config.json"

                def _apply_and_persist(changes: dict, msg: str):
                    # Apply to runtime
                    hp = agent.setdefault("hyperparams", {})
                    hp.update(changes)
                    # reasoning_effort goes top-level too for loader compatibility
                    if "reasoning_effort" in changes:
                        agent["reasoning_effort"] = changes["reasoning_effort"]
                    # Persist
                    try:
                        if cfg_path.exists():
                            c = json.loads(cfg_path.read_text())
                            c.setdefault("hyperparams", {})
                            c["hyperparams"].update(changes)
                            if "reasoning_effort" in changes:
                                c["reasoning_effort"] = changes["reasoning_effort"]
                            cfg_path.write_text(json.dumps(c, indent=2, ensure_ascii=False))
                    except Exception as e:
                        logger.warning("Preset persist partial fail: {}", e)
                    return msg

                if preset == "balanced":
                    # Clear heavy overrides, set medium
                    for k in list(agent.get("hyperparams", {}).keys()):
                        if k in ("temperature", "top_p"):
                            agent["hyperparams"].pop(k, None)
                    if "hyperparams" in agent and not agent["hyperparams"]:
                        agent.pop("hyperparams", None)
                    agent["reasoning_effort"] = "medium"
                    if cfg_path.exists():
                        try:
                            c = json.loads(cfg_path.read_text())
                            c.pop("hyperparams", None)
                            c["reasoning_effort"] = "medium"
                            cfg_path.write_text(json.dumps(c, indent=2, ensure_ascii=False))
                        except Exception:
                            pass
                    await query.edit_message_text(f"✅ {name} 已设为「平衡」预设（中等思考深度，默认采样）。")
                    await self._show_edit_menu(query, name)
                    return

                elif preset == "creative":
                    res = _apply_and_persist(
                        {"temperature": 0.9, "top_p": 0.95, "reasoning_effort": "medium"},
                        "更有创意"
                    )
                    await query.edit_message_text(f"✅ {name} 已应用「{preset}」预设：更高随机性 + 中等思考深度。")
                    await self._show_edit_menu(query, name)
                    return

                elif preset == "precise":
                    res = _apply_and_persist(
                        {"temperature": 0.2, "top_p": 0.9, "reasoning_effort": "medium"},
                        "更严谨"
                    )
                    await query.edit_message_text(f"✅ {name} 已应用「{preset}」预设：低温度严谨采样 + 中等思考。")
                    await self._show_edit_menu(query, name)
                    return

                elif preset == "deep":
                    res = _apply_and_persist(
                        {"temperature": 0.5, "top_p": 0.9, "reasoning_effort": "high"},
                        "深度思考"
                    )
                    await query.edit_message_text(
                        f"✅ {name} 已应用「深度思考」预设：高思考强度（适合支持推理的模型）+ 适中采样。"
                    )
                    await self._show_edit_menu(query, name)
                    return

                elif preset == "reset":
                    agent.pop("hyperparams", None)
                    agent.pop("reasoning_effort", None)
                    if cfg_path.exists():
                        try:
                            c = json.loads(cfg_path.read_text())
                            c.pop("hyperparams", None)
                            c.pop("reasoning_effort", None)
                            cfg_path.write_text(json.dumps(c, indent=2, ensure_ascii=False))
                        except Exception:
                            pass
                    await query.edit_message_text(f"✅ {name} 已恢复默认（清除超参数与思考深度覆盖）。")
                    await self._show_edit_menu(query, name)
                    return

            elif data.startswith("tf:"):
                # tf:AgentName:tool_name — toggle individual tool
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                name, tool = parts[1], parts[2]
                from nanobot.groupchat.orchestra.engine import GroupChatEngine
                agent = self._groupchat_engine.registry.get(name, {})
                tools_cfg = agent.get("tools")
                if not isinstance(tools_cfg, dict) or "web_search" not in tools_cfg:
                    # Legacy or missing config — rebuild from tools_enabled flag
                    all_on = agent.get("tools_enabled", False)
                    tools_cfg = {t: all_on for t in GroupChatEngine.TOOL_NAMES}
                    agent["tools"] = tools_cfg

                if tool == "__all_on":
                    for t in tools_cfg:
                        tools_cfg[t] = True
                elif tool == "__all_off":
                    for t in tools_cfg:
                        tools_cfg[t] = False
                elif tool in tools_cfg:
                    tools_cfg[tool] = not tools_cfg[tool]

                # Persist to config.json
                agent_entry = self._groupchat_engine.registry.get(name, {})
                if agent_entry.get("_default"):
                    # Default agent (Nanobot): save tool toggles to separate file
                    tools_path = Path.home() / ".nanobot" / "nanobot_tools.json"
                    try:
                        tools_path.write_text(json.dumps(tools_cfg, indent=2, ensure_ascii=False))
                    except Exception:
                        pass
                else:
                    cfg_path = Path.home() / ".nanobot" / "agents" / name.lower() / "config.json"
                    cfg_path.parent.mkdir(parents=True, exist_ok=True)
                    cfg = {}
                    if cfg_path.exists():
                        try:
                            cfg = json.loads(cfg_path.read_text())
                        except Exception:
                            pass
                    cfg["tools"] = tools_cfg
                    try:
                        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                    except Exception:
                        pass

                # Refresh buttons by re-triggering tools menu
                labels = {
                    "web_search": "🔍 网页搜索", "web_fetch": "🌐 网页抓取",
                    "exec": "⚡ 执行命令", "read_file": "📄 读文件",
                    "write_file": "✍️ 写文件", "edit_file": "✂️ 编辑文件",
                    "list_dir": "📁 列目录",
                    "memory_palace": "🧠 记忆宫殿",
                }
                buttons = []
                for t in GroupChatEngine.TOOL_NAMES:
                    on = tools_cfg.get(t, False)
                    icon = "✅" if on else "❌"
                    label = labels.get(t, t)
                    buttons.append([InlineKeyboardButton(
                        f"{icon} {label}", callback_data=f"tf:{name}:{t}"
                    )])
                buttons.append([InlineKeyboardButton("✅ 全开", callback_data=f"tf:{name}:__all_on"),
                                InlineKeyboardButton("❌ 全关", callback_data=f"tf:{name}:__all_off")])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])
                await query.edit_message_text(
                    f"🔧 {name} 工具权限设置:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif data.startswith("srr:"):
                # srr:AgentName:rank — set agent rank (basic/standard/advanced/expert only)
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                name, rank_val = parts[1], parts[2]

                from nanobot.groupchat.display.visibility import RANK_DISPLAY, RANK_ORDER

                MODERN_RANKS = list(RANK_ORDER.keys())
                if rank_val not in MODERN_RANKS:
                    return

                agent = self._groupchat_engine.registry.get(name, {})
                old_rank = agent.get("rank")
                agent["rank"] = rank_val

                # Persist to config.json
                cfg_path = Path.home() / ".nanobot" / "agents" / name.lower() / "config.json"
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                cfg = {}
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text())
                    except Exception:
                        pass
                cfg["rank"] = rank_val
                try:
                    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                except Exception:
                    pass

                try:
                    eng = self._groupchat_engine
                    if eng and eng.is_running:
                        eng.refresh_interrupt_ranks()
                        logger.info("Live rank update for interrupt hierarchy: {} -> {}", name, rank_val)
                except Exception as e:
                    logger.debug("Live rank refresh skipped: {}", e)

                buttons = []
                for r in MODERN_RANKS:
                    icon = "✅ " if r == rank_val else "  "
                    buttons.append([InlineKeyboardButton(
                        f"{icon}{RANK_DISPLAY[r]}", callback_data=f"srr:{name}:{r}"
                    )])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")])

                note = ""
                if old_rank and old_rank != rank_val:
                    note = (
                        "\n\n⚠️ 中断权限已更新；对话池/搜索额度在本轮内不变，新广播轮次生效。"
                    )
                await query.edit_message_text(
                    f"🎖️ {name} 等级已设为 {RANK_DISPLAY[rank_val]}{note}",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif data.startswith("log_pg:"):
                page = int(data[7:])
                logs = self._groupchat_engine.request_log
                text, markup = self._build_log_page_v2(logs, page)
                await query.edit_message_text(text, reply_markup=markup)

            elif data.startswith("rlog_pg:"):
                page = int(data[8:])
                logs = self._load_request_logs()
                text, markup = self._build_log_page_v2(logs, page)
                await query.edit_message_text(text, reply_markup=markup)

            elif data.startswith("rlogs_pg:"):
                # Search-filtered pagination
                page = int(data[9:])
                logs = self._load_request_logs()
                kw = getattr(self, "_log_search", {}).get(chat_id, "")
                if kw:
                    logs = self._filter_logs(logs, kw)
                text, markup = self._build_log_page_v2(logs, page, keyword=kw)
                await query.edit_message_text(text, reply_markup=markup)

            elif data.startswith("rlogp:"):
                # Persistent log prompt viewer: rlogp:<idx>:<msg_page>
                parts = data[6:].split(":")
                idx = int(parts[0])
                msg_page = int(parts[1]) if len(parts) > 1 else 0
                logs = self._load_request_logs()
                if idx >= len(logs):
                    await query.edit_message_text("⚠️ 记录不存在")
                    return
                r = logs[idx]
                msgs = r.get("messages", [])
                if not msgs:
                    await query.edit_message_text("📭 无消息记录")
                    return

                per_page = 2
                total_pages = max(1, (len(msgs) + per_page - 1) // per_page)
                msg_page = max(0, min(msg_page, total_pages - 1))
                start_m = msg_page * per_page
                end_m = min(start_m + per_page, len(msgs))

                model_short = (r.get("model") or "?").split("/")[-1][:20]
                lines = [f"📝 请求内容 #{idx+1} {model_short} (第{msg_page+1}/{total_pages}页, 共{len(msgs)}条消息)\n"]
                for mi in range(start_m, end_m):
                    m = msgs[mi]
                    role = m.get("role", "?")
                    name = m.get("name", "")
                    c_len = m.get("content_len", 0)
                    content = m.get("content")
                    tc_id = m.get("tool_call_id", "")
                    name_str = f" [{name}]" if name else ""
                    tc_str = f" tcid={tc_id[:9]}" if tc_id else ""
                    lines.append(f"── [{mi+1}] {role}{name_str} ({c_len}字){tc_str} ──")
                    if isinstance(content, str) and content:
                        lines.append(content[:800])
                        if len(content) > 800:
                            lines.append(f"…(还有{len(content)-800}字)")
                    elif isinstance(content, list):
                        # Content blocks
                        for block in content[:3]:
                            if isinstance(block, dict):
                                lines.append(str(block.get("text", ""))[:400])
                    elif content is None:
                        lines.append("(null)")
                    else:
                        lines.append("(空)")
                    if m.get("tool_calls"):
                        tc_list = m["tool_calls"]
                        for tc in (tc_list if isinstance(tc_list, list) else []):
                            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                            lines.append(f"  🔧 {fn.get('name', '?')}({str(fn.get('arguments', ''))[:100]})")
                    lines.append("")

                text = "\n".join(lines)
                nav = []
                if msg_page > 0:
                    nav.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"rlogp:{idx}:{msg_page-1}"))
                if msg_page < total_pages - 1:
                    nav.append(InlineKeyboardButton("下页 ➡️", callback_data=f"rlogp:{idx}:{msg_page+1}"))
                buttons = []
                if nav:
                    buttons.append(nav)
                buttons.append([InlineKeyboardButton("⬅️ 返回详情", callback_data=f"rlog:{idx}")])
                await query.edit_message_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))

            elif data.startswith("rlog_dl:"):
                # Download full log as JSON file — includes live context snapshot
                idx = int(data[8:])
                logs = self._load_request_logs()
                if idx >= len(logs):
                    await query.answer("⚠️ 记录不存在")
                    return
                r = logs[idx]
                import io as _io
                import datetime as _dt

                # ── Build live context snapshot for each active agent ──
                context_snapshot: dict = {}
                engine = self._groupchat_engine
                if engine:
                    # PromptBuilder already imported at module top level
                    raw_history = engine.history.messages
                    active = engine.active_agents
                    registry = getattr(engine, "registry", {})
                    leader = engine.leader
                    round_num = getattr(engine, "_round", 0)

                    context_snapshot["raw_history"] = raw_history
                    context_snapshot["raw_history_count"] = len(raw_history)
                    context_snapshot["raw_history_chars"] = sum(
                        len(m.get("content", "")) for m in raw_history
                    )
                    context_snapshot["active_agents"] = active
                    context_snapshot["leader"] = leader
                    context_snapshot["round"] = round_num
                    context_snapshot["per_agent_compiled"] = {}
                    context_snapshot["per_agent_validation"] = {}

                    for agent_name in active:
                        if agent_name not in registry:
                            continue
                        try:
                            compiled = PromptBuilder.history_to_messages(
                                raw_history,
                                current_agent=agent_name,
                            )
                            validation = PromptBuilder._validate_context(
                                compiled, agent_name
                            )
                            context_snapshot["per_agent_compiled"][agent_name] = [
                                {
                                    "role": m.get("role"),
                                    "name": m.get("name"),
                                    "content_len": len(m.get("content") or ""),
                                    "content_preview": (m.get("content") or "")[:300],
                                    "has_tool_calls": bool(m.get("tool_calls")),
                                    "tool_call_id": m.get("tool_call_id"),
                                }
                                for m in compiled
                            ]
                            context_snapshot["per_agent_validation"][agent_name] = validation
                        except Exception as snap_err:
                            context_snapshot["per_agent_compiled"][agent_name] = f"ERROR: {snap_err}"
                            context_snapshot["per_agent_validation"][agent_name] = [str(snap_err)]

                # ── Merge into output ──
                output = dict(r)
                output["__context_snapshot__"] = context_snapshot
                output["__snapshot_ts__"] = _dt.datetime.now().isoformat()

                agent = (r.get("agent") or "unknown").replace("/", "_")[:20]
                ts_str = (r.get("ts") or "unknown").replace(" ", "_").replace(":", "")
                filename = f"log_{idx+1}_{agent}_{ts_str}.json"
                content = json.dumps(output, ensure_ascii=False, indent=2, default=str)
                buf = _io.BytesIO(content.encode("utf-8"))
                buf.name = filename
                await query.answer("📤 正在发送文件…")
                agent_count = len(context_snapshot.get("active_agents", []))
                history_count = context_snapshot.get("raw_history_count", 0)
                await query.message.reply_document(
                    document=buf,
                    filename=filename,
                    caption=(
                        f"📋 请求日志 #{idx+1} — {r.get('agent', '?')} [{(r.get('model') or '?').split('/')[-1][:20]}]\n"
                        f"📊 上下文快照: {history_count}条历史 / {agent_count}个Agent已编译"
                    ),
                )

            elif data.startswith("rlog:"):
                # Persistent log detail — brief summary + download confirmation
                idx = int(data[5:])
                logs = self._load_request_logs()
                if idx >= len(logs):
                    await query.edit_message_text("⚠️ 记录不存在")
                    return
                r = logs[idx]

                model = (r.get("model") or "?").split("/")[-1][:25]
                agent = r.get("agent") or "?"
                ts = r.get("ts") or "?"
                status = "✅" if r.get("status") == "ok" else "❌"
                latency = r.get("latency", 0)
                usage = r.get("usage") or {}
                total_tok = usage.get("total", 0)
                cost = r.get("cost")
                cost_str = f"  💰${cost:.4f}" if cost else ""
                msgs = r.get("messages", [])
                msg_count = len(msgs)
                tc_count = r.get("tools_count", 0)
                has_tc = f"  🔧{tc_count}tools" if tc_count else ""
                preview = r.get("reply_preview", "")
                preview_str = f"\n\n[回复预览]\n{preview[:200]}…" if len(preview) > 200 else (f"\n\n[回复预览]\n{preview}" if preview else "")

                # OpenRouter IDs
                gen_id = r.get("generation_id", "")
                req_id = r.get("request_id", "")
                or_prov = r.get("or_provider", "")
                id_lines = ""
                if gen_id:
                    id_lines += f"\n🔑 Generation: {gen_id}"
                if req_id:
                    id_lines += f"\n📋 Request: {req_id}"
                if or_prov:
                    id_lines += f"\n🌐 Provider: {or_prov}"

                # Token breakdown
                prompt_tok = usage.get("prompt", usage.get("prompt_tokens", 0)) or 0
                comp_tok = usage.get("completion", usage.get("completion_tokens", 0)) or 0
                total_chars = r.get("total_chars", 0)
                chars_per_tok = round(total_chars / prompt_tok, 1) if prompt_tok else "?"

                text = (
                    f"📋 请求 #{idx+1}\n"
                    f"{status} {agent} [{model}]\n"
                    f"⏱ {ts}  {latency}s\n"
                    f"📊 {prompt_tok}p + {comp_tok}c = {total_tok}tok  {msg_count}msgs{has_tc}{cost_str}"
                    f"\n📝 输入: {total_chars:,}字 ≈ {prompt_tok}tok ({chars_per_tok}字/tok)"
                    f"{id_lines}"
                    f"{preview_str}"
                )

                page = idx // 8
                buttons = [
                    [InlineKeyboardButton("🔍 上下文 Token 明细", callback_data=f"rlogctx:{idx}:0")],
                    [InlineKeyboardButton("📥 下载完整日志", callback_data=f"rlog_dl:{idx}")],
                    [InlineKeyboardButton("⬅️ 返回列表", callback_data=f"rlog_pg:{page}")],
                ]
                await query.edit_message_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))

            elif data.startswith("rlogctx:"):
                # Per-message token breakdown panel: rlogctx:<idx>:<page>
                parts = data[8:].split(":")
                idx = int(parts[0])
                ctx_page = int(parts[1]) if len(parts) > 1 else 0
                logs = self._load_request_logs()
                if idx >= len(logs):
                    await query.edit_message_text("⚠️ 记录不存在")
                    return
                r = logs[idx]
                msgs = r.get("messages", [])
                usage = r.get("usage") or {}
                prompt_tok = usage.get("prompt", usage.get("prompt_tokens", 0)) or 0
                comp_tok = usage.get("completion", usage.get("completion_tokens", 0)) or 0
                total_tok = usage.get("total", usage.get("total_tokens", 0)) or 0
                total_chars = r.get("total_chars", 0)
                gen_id = r.get("generation_id", "")
                req_id = r.get("request_id", "")

                # Calibrated chars-per-token ratio using real usage data
                if prompt_tok and total_chars:
                    cpt = total_chars / prompt_tok
                else:
                    cpt = 3.5  # conservative default for Chinese+code

                MSGS_PER_PAGE = 5
                total_pages = max(1, (len(msgs) + MSGS_PER_PAGE - 1) // MSGS_PER_PAGE)
                ctx_page = max(0, min(ctx_page, total_pages - 1))
                start_m = ctx_page * MSGS_PER_PAGE
                end_m = min(start_m + MSGS_PER_PAGE, len(msgs))

                agent = r.get("agent") or "?"
                model_short = (r.get("model") or "?").split("/")[-1][:18]

                lines = [
                    f"🔍 上下文Token分析 #{idx+1} {agent}[{model_short}]",
                    f"📊 实际: {prompt_tok}p + {comp_tok}c = {total_tok}tok",
                ]
                if gen_id:
                    lines.append(f"🔑 OR Gen: {gen_id}")
                if req_id:
                    lines.append(f"📋 Req: {req_id}")
                lines.append(f"第{ctx_page+1}/{total_pages}页 | 消息{start_m+1}–{end_m}/{len(msgs)}")
                lines.append("")

                role_icons = {"system": "🟦", "user": "👤", "assistant": "🤖", "tool": "🔧"}
                running_chars = 0

                for mi in range(start_m, end_m):
                    m = msgs[mi]
                    role = m.get("role", "?")
                    icon = role_icons.get(role, "❓")
                    name = m.get("name", "")
                    tc_id = m.get("tool_call_id", "")
                    c_len = m.get("content_len") or len(str(m.get("content") or ""))
                    running_chars += c_len
                    est_tok = round(c_len / cpt)
                    running_tok = round(running_chars / cpt)

                    # Detect special message types
                    content_str = str(m.get("content") or "")
                    markers = []
                    if "[...earlier messages omitted...]" in content_str:
                        markers.append("⚠️省略")
                    if "早期对话摘要" in content_str:
                        markers.append("🗄AI摘要")
                    if m.get("tool_calls"):
                        tc_list = m["tool_calls"] if isinstance(m["tool_calls"], list) else []
                        names = ",".join(
                            (tc.get("function", {}) if isinstance(tc, dict) else {}).get("name", "?")
                            for tc in tc_list
                        )[:30]
                        markers.append(f"📤调用:{names}")
                    marker_str = " " + " ".join(markers) if markers else ""
                    name_str = f"[{name}]" if name else ""
                    tc_str = f" tc={tc_id[:8]}" if tc_id else ""

                    lines.append(
                        f"[{mi+1:02d}]{icon}{role}{name_str}{tc_str}"
                        f"\n  {c_len}字≈{est_tok}tok | 累计~{running_tok}tok{marker_str}"
                    )
                    # Short content preview
                    preview = content_str[:100].replace("\n", " ").strip()
                    if preview:
                        if len(content_str) > 100:
                            preview += f"…(+{len(content_str)-100}字)"
                        lines.append(f"  ↳ {preview}")
                    lines.append("")

                # Footer accounting
                lines.append("─" * 22)
                est_total = round(total_chars / cpt)
                delta = prompt_tok - est_total
                lines.append(f"估算: {total_chars:,}字 ÷ {cpt:.1f} = ~{est_total:,}tok")
                if prompt_tok:
                    sign = "+" if delta >= 0 else ""
                    lines.append(f"实际: {prompt_tok}tok  差值: {sign}{delta}tok (工具定义/格式开销)")
                if gen_id:
                    lines.append("💡 可在 openrouter.ai/activity 用Generation ID查询")

                text = "\n".join(lines)
                nav = []
                if ctx_page > 0:
                    nav.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"rlogctx:{idx}:{ctx_page-1}"))
                if ctx_page < total_pages - 1:
                    nav.append(InlineKeyboardButton("下页 ➡️", callback_data=f"rlogctx:{idx}:{ctx_page+1}"))
                buttons = []
                if nav:
                    buttons.append(nav)
                buttons.append([InlineKeyboardButton("⬅️ 返回详情", callback_data=f"rlog:{idx}")])
                await query.edit_message_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))

            elif data.startswith("logd:"):
                idx = int(data[5:])
                logs = self._groupchat_engine.request_log
                if idx >= len(logs):
                    await query.edit_message_text("⚠️ 记录不存在")
                    return
                r = logs[idx]
                tokens = r.get("tokens", {})
                calls = r.get("calls", [])
                tools = r.get("tools", [])

                def _trunc(s: str, limit: int) -> str:
                    """Truncate with remaining char count."""
                    if len(s) <= limit:
                        return s
                    return s[:limit] + f"…(还有{len(s)-limit}字)"

                lines = [
                    f"--- LLM Call #{idx+1} ---",
                    f"agent={r.get('agent','?')} model={r.get('model','?')} mode={r.get('mode','?')}",
                    f"time={r.get('time','?')} latency={r.get('latency',0)}s iter={r.get('iterations',1)} max_tokens={r.get('max_tokens','?')}",
                    f"tokens: prompt={tokens.get('prompt',0)} compl={tokens.get('completion',0)} total={tokens.get('total',0)}",
                ]

                # Show HTTP status code if error
                sc = r.get("status_code")
                if sc:
                    lines.append(f"http_status={sc}")

                # Sampling params — compact single line
                sp = r.get("sampling_params", {})
                if sp:
                    sp_str = " ".join(f"{k}={v}" for k, v in sp.items() if v)
                    if sp_str:
                        lines.append(f"params: {sp_str}")

                # Tools
                tools_avail = r.get("tools_available")
                tool_names_list = r.get("tool_names", [])
                if tools_avail is not None:
                    lines.append(f"tools: {','.join(tool_names_list) if tool_names_list else 'none'} | used: {','.join(tools) if tools else 'none'}")

                # Messages summary — compact
                msgs_snap = r.get("messages_snapshot", [])
                if msgs_snap:
                    role_counts = {}
                    for m in msgs_snap:
                        role = m.get("role", "?")
                        role_counts[role] = role_counts.get(role, 0) + 1
                    total_chars = sum(m.get("content_len", 0) for m in msgs_snap)
                    rc_str = " ".join(f"{r}={c}" for r, c in role_counts.items())
                    lines.append(f"msgs: {len(msgs_snap)} ({rc_str}) chars={total_chars}")

                # Per-iteration with retry details
                if calls:
                    lines.append("\n[iterations]")
                    for c in calls[:10]:
                        t = c.get("tools", [])
                        t_str = f" tools=[{','.join(t)}]" if t else ""
                        tok = c.get("tokens", {}).get("total_tokens", 0)
                        lines.append(
                            f"  i{c['iter']}: {c.get('latency',0)}s {tok}tok "
                            f"finish={c.get('finish','?')}{t_str}"
                        )
                        # Per-retry details
                        rl = c.get("retry_log", [])
                        for ra in rl:
                            lines.append(
                                f"    retry#{ra['attempt']} [{ra.get('ts','')}] "
                                f"HTTP {ra.get('status','?')} wait={ra.get('delay',0)}s "
                                f"err={_trunc(ra.get('error',''), 80)}"
                            )

                # Tool call details
                tcd = r.get("tool_calls_detail", [])
                if tcd:
                    lines.append(f"\n[tool_calls] ({len(tcd)})")
                    for i, tc in enumerate(tcd[:10]):
                        ts = tc.get("timestamp", "")
                        dur = tc.get("duration", "?")
                        ok = tc.get("success", True)
                        status = "OK" if ok else "FAIL"
                        lines.append(f"  {i+1}. [{ts}] {tc['name']} i{tc.get('iteration','?')} {dur}s {status}")
                        lines.append(f"     args={_trunc(tc.get('args',''), 100)}")
                        if ok:
                            rp = tc.get('result_preview', '')
                            if rp:
                                lines.append(f"     => {_trunc(rp.replace(chr(10), ' '), 100)} ({tc.get('result_len',0)}字)")
                        else:
                            lines.append(f"     err={_trunc(tc.get('error',''), 120)}")
                    if len(tcd) > 10:
                        lines.append(f"  ...+{len(tcd)-10} more")

                # I/O
                inp = r.get("input_preview", "")
                out = r.get("output", "")
                if inp:
                    lines.append(f"\n[input] {_trunc(inp, 200)}")
                if out:
                    lines.append(f"[output] {_trunc(out, 300)}")
                if r.get("error"):
                    lines.append(f"[error] {_trunc(r['error'], 300)}")
                lines.append(f"reply_len={r.get('reply_len', 0)}")
                text = "\n".join(lines)
                page = idx // 8
                buttons = []
                if msgs_snap:
                    buttons.append([InlineKeyboardButton("📝 完整 Prompt", callback_data=f"logp:{idx}:0")])
                buttons.append([InlineKeyboardButton("⬅️ 返回列表", callback_data=f"log_pg:{page}")])
                await query.edit_message_text(
                    text[:4096],
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif data.startswith("logp:"):
                # Prompt detail viewer: logp:<log_idx>:<msg_page>
                parts = data[5:].split(":")
                idx = int(parts[0])
                msg_page = int(parts[1]) if len(parts) > 1 else 0
                logs = self._groupchat_engine.request_log
                if idx >= len(logs):
                    await query.edit_message_text("⚠️ 记录不存在")
                    return
                r = logs[idx]
                msgs_snap = r.get("messages_snapshot", [])
                if not msgs_snap:
                    await query.edit_message_text("📭 无消息记录")
                    return

                per_page = 3  # messages per page
                total_pages = max(1, (len(msgs_snap) + per_page - 1) // per_page)
                msg_page = max(0, min(msg_page, total_pages - 1))
                start = msg_page * per_page
                end = min(start + per_page, len(msgs_snap))

                lines = [f"📝 Prompt 详情 #{idx+1} (第{msg_page+1}/{total_pages}页, 共{len(msgs_snap)}条消息)\n"]
                for i in range(start, end):
                    m = msgs_snap[i]
                    role = m.get("role", "?")
                    name = m.get("name", "")
                    content_len = m.get("content_len", 0)
                    content = m.get("content", "")
                    name_str = f" [{name}]" if name else ""
                    lines.append(f"── [{i+1}] {role}{name_str} ({content_len}字) ──")
                    # Show content, truncated to fit in Telegram
                    if content:
                        lines.append(content[:800])
                    else:
                        lines.append("(空)")
                    lines.append("")

                text = "\n".join(lines)
                nav = []
                if msg_page > 0:
                    nav.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"logp:{idx}:{msg_page-1}"))
                if msg_page < total_pages - 1:
                    nav.append(InlineKeyboardButton("下页 ➡️", callback_data=f"logp:{idx}:{msg_page+1}"))
                buttons = []
                if nav:
                    buttons.append(nav)
                buttons.append([InlineKeyboardButton("⬅️ 返回详情", callback_data=f"logd:{idx}")])
                await query.edit_message_text(
                    text[:4096],
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif data.startswith("sl:"):
                name = data[3:]
                result = self._groupchat_engine.set_leader(name)
                await query.edit_message_text(result)

            elif data.startswith("lg:"):
                name = data[3:]
                self._ensure_gc_send(chat_id)
                result = self._groupchat_engine.load_group(name)
                await query.edit_message_text(result)

            elif data.startswith("dg:"):
                name = data[3:]
                result = self._groupchat_engine.delete_group(name)
                await query.edit_message_text(result)

            elif data.startswith("hp:"):
                key = data[3:]
                provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
                params = getattr(provider, 'sampling_params', None) if provider else None
                if params and key in params:
                    self._edit_state[chat_id] = {"field": "hp_value", "hp_key": key}
                    await query.edit_message_text(
                        f"✏️ 修改全局 {key}\n"
                        f"当前值: {params[key]}\n\n"
                        "请输入新值（数字）。\n"
                        "示例：temperature 常用 0.2~1.0（越高越有创意但越不稳定）\n"
                        "新手建议不要随意改，先试「思考深度」功能。"
                    )

            elif data.startswith("hp_del:"):
                key = data[7:]
                provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
                params = getattr(provider, 'sampling_params', None) if provider else None
                if params and key in params:
                    del params[key]
                    # Persist
                    hp_path = Path.home() / ".nanobot" / "hyperparams.json"
                    try:
                        hp_path.write_text(json.dumps(params, indent=2))
                        logger.info("Persisted hyperparams (del {}) to {}", key, hp_path)
                    except Exception as e:
                        logger.error("Failed to persist hyperparams: {}", e)
                        await self._gc_send(chat_id, f"⚠️ 参数已生效但持久化失败: {e}")
                    await query.edit_message_text(f"🗑 已删除 {key}")
                    await self._send_hyperparams_keyboard(chat_id, params)

            elif data == "hp_add":
                # Show common params to add
                provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
                params = getattr(provider, 'sampling_params', None) if provider else {}
                common = ["temperature", "top_p", "top_k", "min_p", "top_a",
                          "frequency_penalty", "presence_penalty", "repetition_penalty"]
                available = [p for p in common if p not in params]
                buttons = []
                for p in available:
                    buttons.append([InlineKeyboardButton(f"➕ {p}", callback_data=f"hp_new:{p}")])
                buttons.append([InlineKeyboardButton("✏️ 自定义参数名", callback_data="hp_custom")])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="hp_back")])
                await query.edit_message_text(
                    "➕ 选择要添加的参数（全局）：\n仅推荐给清楚这些参数含义的用户。temperature / top_p 是最常用，其余属于进阶。",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif data.startswith("hp_new:"):
                key = data[7:]
                self._edit_state[chat_id] = {"field": "hp_value", "hp_key": key, "hp_is_new": True}
                await query.edit_message_text(
                    f"➕ 添加全局 {key}\n\n"
                    "请输入值（数字）。\n"
                    "⚠️ 只有当你知道这个参数具体影响时再添加。\n"
                    "temperature/top_p 是最常见的两个；其他如 min_p、top_k 等属于进阶用法。"
                )

            elif data == "hp_custom":
                self._edit_state[chat_id] = {"field": "hp_add_custom"}
                await query.edit_message_text("✏️ 请输入参数名（例如 temperature）。\n只有熟悉采样参数的用户才需要自定义，普通用户建议取消并使用「思考深度」。")

            elif data == "hp_back":
                provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
                params = getattr(provider, 'sampling_params', None) if provider else {}
                await query.edit_message_text("⚙️ 返回...")
                await self._send_hyperparams_keyboard(chat_id, params)

            # ── Agent Hyperparams (ahp:) ──────────────────────────
            elif data.startswith("ahp:"):
                # ahp:AgentName:key
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                a_name, key = parts[1], parts[2]
                agent = self._groupchat_engine.registry.get(a_name, {}) if self._groupchat_engine else {}
                agent_hp = agent.get("hyperparams") or {}
                if key in agent_hp:
                    self._edit_state[chat_id] = {"field": "ahp_value", "agent": a_name, "hp_key": key}
                    await query.edit_message_text(
                        f"✏️ 修改 {a_name} 的 {key}\n"
                        f"当前值: {agent_hp[key]}\n\n"
                        "请输入新值（数字）。此设置仅对此 agent 生效，会覆盖全局。\n"
                        "示例：temperature 0.7 左右平衡创意与可靠；0.2 更严谨。\n"
                        "提示：大多数情况留空或只用「思考深度」按钮更简单。"
                    )

            elif data.startswith("ahp_del:"):
                # ahp_del:AgentName:key
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                a_name, key = parts[1], parts[2]
                if self._groupchat_engine and a_name in self._groupchat_engine.registry:
                    agent = self._groupchat_engine.registry[a_name]
                    agent_hp = agent.get("hyperparams") or {}
                    if key in agent_hp:
                        del agent_hp[key]
                        agent["hyperparams"] = agent_hp
                        # Persist to config.json
                        cfg_path = Path.home() / ".nanobot" / "agents" / a_name.lower() / "config.json"
                        if cfg_path.exists():
                            try:
                                cfg = json.loads(cfg_path.read_text())
                                cfg.setdefault("hyperparams", {})
                                cfg["hyperparams"].pop(key, None)
                                cfg_path.write_text(json.dumps(cfg, indent=2))
                            except Exception as e:
                                logger.error("Failed to persist agent hyperparams: {}", e)
                        await query.edit_message_text(f"🗑 已删除 {a_name} 的 {key}")
                        await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent_hp)

            elif data.startswith("ahp_sync:"):
                # ahp_sync:AgentName
                a_name = data[9:]
                if self._groupchat_engine and a_name in self._groupchat_engine.registry:
                    global_hp = {}
                    hp_path = Path.home() / ".nanobot" / "hyperparams.json"
                    if hp_path.exists():
                        try:
                            saved = json.loads(hp_path.read_text())
                            if isinstance(saved, dict):
                                global_hp = saved
                        except Exception:
                            pass
                    if not global_hp:
                        provider = getattr(self._groupchat_engine, 'provider', None)
                        if provider and hasattr(provider, 'sampling_params'):
                            global_hp = dict(provider.sampling_params)

                    if global_hp:
                        agent = self._groupchat_engine.registry[a_name]
                        agent_hp = agent.get("hyperparams") or {}
                        agent_hp.update(global_hp)
                        agent["hyperparams"] = agent_hp
                        # Note: editagent 超参数修改无需重启/新命令。
                        # 直接 mutate 活的 registry + 磁盘；_chat_with_tools 在调用前
                        # 现读 registry（见 engine.py），下一次该 agent turn 自动生效。
                        cfg_path = Path.home() / ".nanobot" / "agents" / a_name.lower() / "config.json"
                        if cfg_path.exists():
                            try:
                                cfg = json.loads(cfg_path.read_text())
                                cfg["hyperparams"] = agent_hp
                                cfg_path.write_text(json.dumps(cfg, indent=2))
                            except Exception as e:
                                logger.error("Failed to persist agent hyperparams: {}", e)
                        await query.answer("✅ 已复制全局超参数")
                        await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent_hp)
                    else:
                        await query.answer("⚠️ 全局超参数为空", show_alert=True)

            elif data.startswith("ahp_add:"):
                # ahp_add:AgentName
                a_name = data[8:]
                agent = self._groupchat_engine.registry.get(a_name, {}) if self._groupchat_engine else {}
                agent_hp = agent.get("hyperparams") or {}
                common = ["temperature", "top_p", "top_k", "min_p", "top_a",
                          "frequency_penalty", "presence_penalty", "repetition_penalty"]
                available = [p for p in common if p not in agent_hp]
                buttons = []
                for p in available:
                    buttons.append([InlineKeyboardButton(f"➕ {p}", callback_data=f"ahp_new:{a_name}:{p}")])
                buttons.append([InlineKeyboardButton("✏️ 自定义参数名", callback_data=f"ahp_custom:{a_name}")])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ahp_back:{a_name}")])
                await query.edit_message_text(
                    f"➕ 为 {a_name} 添加参数：\n这些是高级采样参数。新手强烈建议先只用「思考深度」和工具开关来调整，效果更直观可预测。",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif data.startswith("ahp_new:"):
                # ahp_new:AgentName:key
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                a_name, key = parts[1], parts[2]
                self._edit_state[chat_id] = {"field": "ahp_value", "agent": a_name, "hp_key": key, "hp_is_new": True}
                await query.edit_message_text(
                    f"➕ 为 {a_name} 添加 {key}\n\n"
                    "请输入值（数字）。此值仅影响该 agent。\n"
                    "推荐：除非必要，否则先用该 agent 的「思考深度」和「工具权限」来调整行为，更直观。"
                )

            elif data.startswith("ahp_custom:"):
                # ahp_custom:AgentName
                a_name = data[11:]
                self._edit_state[chat_id] = {"field": "ahp_add_custom", "agent": a_name}
                await query.edit_message_text("✏️ 请输入参数名（例如 temperature）。\n只有熟悉采样参数的用户才需要自定义，普通用户建议取消并使用该 agent 的「思考深度」。")

            elif data.startswith("ahp_back:"):
                # ahp_back:AgentName
                a_name = data[9:]
                agent = self._groupchat_engine.registry.get(a_name, {}) if self._groupchat_engine else {}
                agent_hp = agent.get("hyperparams") or {}
                await query.edit_message_text("⚙️ 返回...")
                await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent_hp)

            elif data.startswith("gc:"):
                key = data[3:]
                settings = self._load_gc_settings()
                label = self.GC_SETTINGS_LABELS.get(key, key)
                val = settings.get(key, self.GC_SETTINGS_DEFAULTS.get(key, "?"))
                self._edit_state[chat_id] = {"field": "gc_value", "gc_key": key}
                await query.edit_message_text(
                    f"✏️ 修改 {label}\n"
                    f"当前值: {val}\n\n"
                    f"请输入新值 (整数):"
                )

            elif data.startswith("ord:"):
                val = data[4:]
                if val == "done":
                    agents = self._groupchat_engine.active_agents
                    # Persist final order
                    self._groupchat_engine.save_active()
                    # Auto-update saved group
                    gname = getattr(self._groupchat_engine, '_current_group_name', None)
                    if gname:
                        groups = self._groupchat_engine.load_groups()
                        if gname in groups:
                            groups[gname] = list(agents)
                            self._groupchat_engine.save_groups(groups)
                    order_str = " → ".join(agents)
                    await query.edit_message_text(f"📢 发言顺序:\n{order_str}")
                else:
                    idx = int(val)
                    agents = self._groupchat_engine.active_agents
                    if 0 < idx < len(agents):
                        # Swap with previous
                        agents[idx], agents[idx-1] = agents[idx-1], agents[idx]
                        self._groupchat_engine.reorder_agents(list(agents))
                    # Refresh keyboard
                    await query.edit_message_text("📢 更新中...")
                    await self._send_order_keyboard(chat_id, self._groupchat_engine.active_agents)

            # ── Prompt orchestration callbacks ──
            elif data == "prmanage":
                # Toggle manage mode for prompt order view
                self._prompt_manage_mode = not getattr(self, '_prompt_manage_mode', False)
                await self._prompt_show_components(query, manage_mode=self._prompt_manage_mode)

            elif data in ("pr:refresh", "pr:"):
                # Refresh global prompt order view (exits manage mode)
                self._prompt_manage_mode = False
                await self._prompt_show_components(query)

            elif data.startswith("pre:"):
                # Edit global template: pre:__global__:component_key
                parts = data[4:].split(":", 1)
                if len(parts) == 2:
                    _, key = parts
                    engine = self._groupchat_engine
                    content = PromptBuilder.get_component_template(key)
                    label = _COMPONENT_LABELS.get(key, key)
                    phase = _COMPONENT_PHASES.get(key, "static")
                    phase_label = "STATIC" if phase == "static" else "DYNAMIC"
                    vars_hint = (
                        "{{agent}} {{members}} {{tools}} {{others}} {{identity}}"
                        if phase == "static"
                        else "{{agent}} {{members}} {{tools}} {{others}} {{identity}} {{datetime}} {{round}} {{agent_idx}} {{total}} {{teammates}}"
                    )
                    self._edit_state[chat_id] = {"field": "prompt_edit", "agent": "__global__", "key": key}
                    preview = (content[:3500] + "…") if len(content) > 3500 else (content or "(空)")
                    await query.edit_message_text(
                        f"✏️ 编辑全局模板 - {label} [{phase_label}]\n\n"
                        f"当前内容 ({len(content or '')}字):\n"
                        f"{preview}\n\n"
                        f"💡 可用变量: {vars_hint}\n"
                        f"请回复新内容 (完整替换):",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌ 取消", callback_data="prcan")]
                        ]),
                    )

            elif data == "prcan":
                # Cancel edit
                self._edit_state.pop(chat_id, None)
                await self._prompt_show_components(query, manage_mode=getattr(self, '_prompt_manage_mode', False))

            elif data.startswith("pru:") or data.startswith("prd:"):
                # Move component up/down: pru:<idx> or prd:<idx>
                direction = -1 if data.startswith("pru:") else 1
                idx = int(data[4:])
                engine = self._groupchat_engine
                order = engine.prompt_builder.get_agent_prompt_order()
                new_idx = idx + direction
                if 0 <= new_idx < len(order):
                    order[idx], order[new_idx] = order[new_idx], order[idx]
                    engine.prompt_builder.set_default_prompt_order(order)
                await self._prompt_show_components(query, manage_mode=getattr(self, '_prompt_manage_mode', False))

            elif data.startswith("pviz:"):
                # Toggle visibility: pviz:<idx>
                # Note: query.answer() has already been called globally above.
                idx = int(data[5:])
                engine = self._groupchat_engine
                order = engine.prompt_builder.get_agent_prompt_order()
                if 0 <= idx < len(order):
                    key = order[idx]
                    result = engine.prompt_builder.toggle_component_visibility(key)
                    vis = engine.prompt_builder.get_component_visibility(key)
                    vis_label = "全体可见 👁" if vis == "all" else "仅Leader可见 👑"
                    logger.debug("pviz toggle: {} → {}", key, vis_label)
                await self._prompt_show_components(query, manage_mode=getattr(self, '_prompt_manage_mode', False))

            elif data.startswith("prdel:"):
                # Delete component: prdel:<idx>
                idx = int(data[6:])
                engine = self._groupchat_engine
                result = engine.prompt_builder.remove_prompt_component(idx)
                await query.answer(result, show_alert=True)
                await self._prompt_show_components(query, manage_mode=getattr(self, '_prompt_manage_mode', False))

            elif data == "pradd":
                # Show available components to add back
                engine = self._groupchat_engine
                available = engine.prompt_builder.get_available_components()
                buttons = []
                labels = _COMPONENT_LABELS
                for key in available:
                    buttons.append([InlineKeyboardButton(
                        f"➕ {labels.get(key, key)}",
                        callback_data=f"pradd:{key}"
                    )])
                buttons.append([InlineKeyboardButton(
                    "✏️ 自定义组件名", callback_data="pradd_custom"
                )])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="pr:refresh")])
                await query.edit_message_text(
                    "➕ 选择要添加的组件:\n\n💡 点击 \"✏️ 自定义组件名\" 创建全新组件",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            elif data.startswith("pradd:"):
                # Add component back: pradd:<key>
                key = data[6:]
                engine = self._groupchat_engine
                order = engine.prompt_builder.get_agent_prompt_order()
                if key not in order:
                    order.append(key)
                    engine.prompt_builder.set_default_prompt_order(order)
                await self._prompt_show_components(query)

            elif data == "pradd_custom":
                # Enter edit state for user to type a custom component name
                chat_id = str(query.message.chat_id)
                self._edit_state[chat_id] = {"field": "pradd_custom_name"}
                await query.edit_message_text(
                    "✏️ 创建自定义提示词组件\n\n"
                    "请输入组件名称（如: 角色背景、安全规则、写作风格 等）:\n\n"
                    "💡 名称会显示在组件列表中，创建后可选 Phase 类型",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ 取消", callback_data="prcan")]
                    ]),
                )

            elif data == "prrules":
                # Show prompt assembly rules explanation
                rules_text = (
                    "📖 Prompt 组装规则\n\n"
                    "▸ 组装顺序\n"
                    "  STATIC → 💬 HISTORY → DYNAMIC\n"
                    "  静态位于 history 前，动态位于 history 后\n\n"
                    "▸ Phase 区别\n\n"
                    "  STATIC\n"
                    "    位于聊天记录之前，所有 agent 共享\n"
                    "    仅可使用 stable vars（不随轮次变化）\n"
                    "    适合: 人设、工具指令、硬规则等固定内容\n"
                    "    缓存友好，LLM 可复用前缀\n\n"
                    "  DYNAMIC\n"
                    "    位于聊天记录之后，每轮刷新\n"
                    "    可使用 stable + volatile vars\n"
                    "    适合: 群聊上下文、示例、技能概览等时效内容\n\n"
                    "▸ 可用变量\n"
                    "  stable（通用）:\n"
                    "    {{agent}} {{members}} {{tools}} {{others}} {{identity}}\n\n"
                    "  volatile（仅 dynamic 可用）:\n"
                    "    {{datetime}} {{round}} {{agent_idx}} {{total}} {{teammates}}\n\n"
                    "▸ 组件来源\n"
                    "  ✏️ 全局模板 — /prompt 编辑可修改内容\n"
                    "  📂 per-agent — /editagent 编辑，各 agent 独立\n"
                    "  🔒 代码生成 — 不可编辑（history、skills_overview）\n\n"
                    "▸ 内容状态\n"
                    "  ● 已配置（注入提示词）\n"
                    "  ○ 空（跳过注入）\n"
                    "  [条件] 按条件激活（如仅 Leader 可见）\n\n"
                    "▸ 组件可见性\n"
                    "  👁 全体可见 — 所有 agent 均可读取\n"
                    "  👑 仅 Leader 可见 — 普通 agent 不可见"
                )
                await query.edit_message_text(
                    rules_text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ 返回组件列表", callback_data="pr:refresh")]
                    ]),
                )

            elif data.startswith("prv:"):
                # Preview full template: prv:<page>
                page = int(data[4:])
                engine = self._groupchat_engine
                order = engine.prompt_builder.get_agent_prompt_order()
                labels = _COMPONENT_LABELS

                phases = _COMPONENT_PHASES
                lines: list[str] = [
                    "📌 组装规则: STATIC → 💬聊天记录 → DYNAMIC",
                    "   stable vars only      stable + volatile vars",
                    "",
                ]
                display_num = 0
                for i, key in enumerate(order):
                    label = labels.get(key, key)
                    phase = phases.get(key, "static")
                    if key == "history":
                        lines.append("")
                        lines.append("  ── CHAT · runtime auto ──")
                        lines.append("")
                        continue

                    # Phase divider
                    if phase == "dynamic" and (i == 0 or phases.get(order[i - 1], "static") == "static"):
                        lines.append("")
                        lines.append("  ── DYNAMIC ──")
                        lines.append("")

                    display_num += 1
                    if key == "persona":
                        lines.append(f"─── [{display_num}] {label} ───")
                        lines.append("(→ 运行时加载每个 agent 的 SOUL.md)")
                        lines.append("")
                        continue
                    tpl = PromptBuilder.get_component_template(key)
                    if not tpl:
                        lines.append(f"─── [{display_num}] {label} ─── ○ 空 (跳过注入)")
                        lines.append("")
                        continue
                    phase_tag = "STATIC" if phase == "static" else "DYNAMIC"
                    lines.append(f"─── [{display_num}] {phase_tag} {label} ({len(tpl):,}字) ───")
                    preview = tpl[:400]
                    if len(tpl) > 400:
                        preview += "…"
                    lines.append(preview)
                    lines.append("")

                full_text = "\n".join(lines)
                page_size = 3500
                total_pages = max(1, (len(full_text) + page_size - 1) // page_size)
                start = page * page_size
                end = min(start + page_size, len(full_text))
                page_text = f"🔍 全局 Prompt 模板预览 (第{page+1}/{total_pages}页)\n\n" + full_text[start:end]

                nav = []
                if page > 0:
                    nav.append(InlineKeyboardButton("⬅️ 上页", callback_data=f"prv:{page-1}"))
                if page < total_pages - 1:
                    nav.append(InlineKeyboardButton("下页 ➡️", callback_data=f"prv:{page+1}"))
                buttons = []
                if nav:
                    buttons.append(nav)
                buttons.append([InlineKeyboardButton("⬅️ 返回组件列表", callback_data="pr:refresh")])
                await query.edit_message_text(
                    page_text[:4096],
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            # ── Provider/Model management callbacks ──
            elif data == "pm_cancel":
                self._edit_state.pop(chat_id, None)
                await query.edit_message_text("❌ 已取消")

            elif data == "st_prov":
                await self._speedtest_providers(query.message)

            elif data == "st_agent":
                await self._speedtest_agents(query.message)

            elif data.startswith("pm_newm:"):
                # User picked a provider for /newmodel
                prov = data[8:]
                self._edit_state[chat_id] = {"field": "pm_model_id", "mode": "pm", "provider": prov}
                await query.edit_message_text(
                    f"🏢 提供商: {prov}\n\n"
                    "请输入模型ID (如 google/gemini-3-flash-preview):"
                )

            elif data.startswith("pm_delp:"):
                prov = data[8:]
                pm = self._load_pm()
                model_count = len(pm.get("models", {}).get(prov, []))
                await query.edit_message_text(
                    f"⚠️ 确认删除提供商 **{prov}**？\n\n"
                    f"这将同时删除该提供商下的 **{model_count}** 个模型。\n"
                    f"此操作不可撤销！",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 确认删除", callback_data=f"pm_delp_yes:{prov}")],
                        [InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")],
                    ])
                )

            elif data.startswith("pm_delp_yes:"):
                prov = data[12:]
                pm = self._load_pm()
                pm.get("providers", {}).pop(prov, None)
                pm.get("models", {}).pop(prov, None)
                self._save_pm(pm)
                await query.edit_message_text(f"✅ 提供商 {prov} 及其所有模型已删除")

            elif data.startswith("pm_delm_p:"):
                prov = data[10:]
                pm = self._load_pm()
                models = pm.get("models", {}).get(prov, [])
                if not models:
                    await query.edit_message_text("⚠️ 该提供商没有模型")
                    return
                buttons = [[InlineKeyboardButton(f"🗑 {m}", callback_data=f"pm_delm:{prov}:{m}")] for m in models]
                buttons.append([InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")])
                await query.edit_message_text(f"🗑 删除 {prov} 的模型:", reply_markup=InlineKeyboardMarkup(buttons))

            elif data.startswith("pm_delm:"):
                parts = data.split(":", 2)
                prov, model = parts[1], parts[2]
                pm = self._load_pm()
                if prov in pm.get("models", {}):
                    pm["models"][prov] = [m for m in pm["models"][prov] if m != model]
                self._save_pm(pm)
                try:
                    await query.answer(f"🗑 已删除 {model}", show_alert=False)
                except Exception:
                    pass
                # Refresh model list
                remaining = pm.get("models", {}).get(prov, [])
                if remaining:
                    buttons = [[InlineKeyboardButton(f"🗑 {m}", callback_data=f"pm_delm:{prov}:{m}")] for m in remaining]
                    buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="pm_cancel")])
                    await query.edit_message_text(f"🗑 删除 {prov} 的模型 ({len(remaining)}):", reply_markup=InlineKeyboardMarkup(buttons))
                else:
                    await query.edit_message_text(f"✅ {prov} 的模型已全部删除")

            # ── Edit agent model: 2-step provider → model selection ──
            elif data.startswith("em_prov:"):
                parts = data.split(":", 2)
                agent_name, prov = parts[1], parts[2]
                pm = self._load_pm()
                models = pm.get("models", {}).get(prov, [])
                if not models:
                    self._edit_state[chat_id] = {"agent": agent_name, "field": "model", "provider": prov}
                    await query.edit_message_text(
                        f"🏢 {prov} 暂无已注册模型\n\n"
                        "请直接输入模型ID:"
                    )
                    return
                # Cache models for index-based lookup (avoids 64-byte callback limit)
                if not hasattr(self, "_em_model_cache"):
                    self._em_model_cache = {}
                self._em_model_cache[f"{agent_name}:{prov}"] = models
                buttons = []
                for i, m in enumerate(models):
                    buttons.append([InlineKeyboardButton(f"🤖 {m}", callback_data=f"em_mi:{agent_name}:{prov}:{i}")])
                buttons.append([InlineKeyboardButton("✏️ 手动输入", callback_data=f"em_manual:{agent_name}")])
                await query.edit_message_text(f"🏢 {prov} — 选择模型:", reply_markup=InlineKeyboardMarkup(buttons))

            elif data.startswith("em_mi:") or data.startswith("em_model:"):
                # em_mi:agent:prov:index — resolve model from index cache
                if data.startswith("em_mi:"):
                    parts = data.split(":")
                    agent_name, prov, idx = parts[1], parts[2], int(parts[3])
                    cache = getattr(self, "_em_model_cache", {})
                    models = cache.get(f"{agent_name}:{prov}", [])
                    if idx >= len(models):
                        await query.edit_message_text("⚠️ 模型索引无效，请重新操作")
                        return
                    model = models[idx]
                else:
                    parts = data.split(":", 3)
                    agent_name, prov, model = parts[1], parts[2], parts[3]
                if self._groupchat_engine and agent_name in self._groupchat_engine.registry:
                    self._groupchat_engine.registry[agent_name]["model"] = model
                    # Update config on disk
                    from pathlib import Path as _P
                    agent_entry = self._groupchat_engine.registry[agent_name]
                    if agent_entry.get("_default"):
                        # Default agent (Nanobot): update config.json
                        main_cfg_path = _P.home() / ".nanobot" / "config.json"
                        if main_cfg_path.exists():
                            cfg = json.loads(main_cfg_path.read_text())
                            cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = model
                            main_cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                    else:
                        cfg_path = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "config.json"
                        if cfg_path.exists():
                            cfg = json.loads(cfg_path.read_text())
                            cfg["model"] = model
                            cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                    await query.edit_message_text(f"✅ {agent_name} 模型已更新:\n🏢 {prov} / 🤖 {model}")
                else:
                    await query.edit_message_text(f"❌ Agent '{agent_name}' 不存在")
                self._edit_state.pop(chat_id, None)

            elif data.startswith("em_manual:"):
                agent_name = data[10:]
                self._edit_state[chat_id] = {"agent": agent_name, "field": "model"}
                await query.edit_message_text("请输入新模型名 (如 anthropic/claude-sonnet-4-5):")

            # ── Edit provider callbacks ──
            elif data.startswith("ep_pick:"):
                prov = data[8:]
                pm = self._load_pm()
                info = pm.get("providers", {}).get(prov, {})
                url = info.get("url", "?")
                key_preview = info.get("apiKey", "")[:8] + "..." if info.get("apiKey") else "(none)"
                retry = info.get("retryDelays", [1, 2, 4])
                retry_str = f"{len(retry)}次 ({','.join(str(d) for d in retry)}s)"
                buttons = [
                    [InlineKeyboardButton("🔗 修改 URL", callback_data=f"ep_field:{prov}:url")],
                    [InlineKeyboardButton("🔑 修改 API Key", callback_data=f"ep_field:{prov}:key")],
                    [InlineKeyboardButton(f"🔄 重试策略: {retry_str}", callback_data=f"ep_retry:{prov}")],
                    [InlineKeyboardButton("📋 拉取模型列表", callback_data=f"ep_models:{prov}")],
                    [InlineKeyboardButton("❌ 取消", callback_data="pm_cancel")],
                ]
                await query.edit_message_text(
                    f"✏️ 编辑提供商: {prov}\n\n"
                    f"🔗 URL: {url}\n"
                    f"🔑 Key: {key_preview}\n"
                    f"🔄 重试: {retry_str}",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            elif data.startswith("ep_field:"):
                parts = data.split(":", 2)
                prov, fld = parts[1], parts[2]
                self._edit_state[chat_id] = {"field": f"ep_{fld}", "mode": "pm", "prov_name": prov}
                prompts = {"url": "请输入新的 API Base URL:", "key": "请输入新的 API Key:"}
                await query.edit_message_text(f"✏️ {prov} — {prompts.get(fld, fld)}")

            elif data.startswith("ep_retry:"):
                prov = data[9:]
                # Show retry presets
                presets = {
                    "std": ("标准 3次", [1, 2, 4]),
                    "strong": ("加强 5次", [1, 2, 4, 8, 16]),
                    "max": ("极限 7次", [1, 2, 4, 8, 16, 32, 60]),
                }
                pm = self._load_pm()
                current = pm.get("providers", {}).get(prov, {}).get("retryDelays", [1, 2, 4])
                current_str = f"{len(current)}次 ({','.join(str(d) for d in current)}s)"
                buttons = []
                for key, (label, delays) in presets.items():
                    mark = " ✓" if delays == current else ""
                    buttons.append([InlineKeyboardButton(
                        f"🔄 {label} ({','.join(str(d) for d in delays)}s){mark}",
                        callback_data=f"ep_retry_set:{prov}:{key}",
                    )])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")])
                await query.edit_message_text(
                    f"🔄 {prov} 重试策略\n\n"
                    f"当前: {current_str}\n\n"
                    f"选择预设:",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            elif data.startswith("ep_retry_set:"):
                parts = data.split(":", 2)
                prov, preset = parts[1], parts[2]
                presets = {
                    "std": [1, 2, 4],
                    "strong": [1, 2, 4, 8, 16],
                    "max": [1, 2, 4, 8, 16, 32, 60],
                }
                delays = presets.get(preset, [1, 2, 4])
                pm = self._load_pm()
                if prov in pm.get("providers", {}):
                    pm["providers"][prov]["retryDelays"] = delays
                    self._save_pm(pm)
                # Apply to live provider
                engine = self._groupchat_engine
                if engine and hasattr(engine, "provider"):
                    engine.provider._retry_delays = tuple(delays)
                await query.edit_message_text(
                    f"✅ {prov} 重试策略已更新!\n"
                    f"🔄 {len(delays)}次重试 ({','.join(str(d) for d in delays)}s)\n\n"
                    f"总等待时间: {sum(delays)}s"
                )

            elif data.startswith("ep_models:"):
                prov = data[10:]
                pm = self._load_pm()

                # Use cache if available (for back navigation)
                cache = getattr(self, "_model_cache", {})
                if prov in cache:
                    model_ids = cache[prov]
                else:
                    info = pm.get("providers", {}).get(prov, {})
                    url = info.get("url", "").rstrip("/")
                    api_key = info.get("apiKey", "")
                    if not url or not api_key:
                        await query.edit_message_text(f"⚠️ {prov} 缺少 URL 或 API Key")
                        return
                    # Fetch /v1/models
                    import aiohttp
                    import json as _json
                    if "openrouter" in url.lower():
                        models_url = "https://openrouter.ai/api/v1/models"
                    elif "/v1" in url:
                        models_url = f"{url}/models"
                    else:
                        models_url = f"{url}/v1/models"
                    try:
                        async with aiohttp.ClientSession() as session:
                            headers = {"Authorization": f"Bearer {api_key}"}
                            async with session.get(models_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                                body = await resp.text()
                                if resp.status != 200:
                                    await query.edit_message_text(f"❌ 拉取失败 (HTTP {resp.status})\n{body[:200]}")
                                    return
                                try:
                                    result = _json.loads(body)
                                except Exception:
                                    await query.edit_message_text("❌ 拉取失败: 返回非JSON格式")
                                    return
                    except Exception as e:
                        await query.edit_message_text(f"❌ 拉取失败: {str(e)[:100]}")
                        return

                    model_list = result.get("data", []) if isinstance(result, dict) else []
                    if not model_list:
                        await query.edit_message_text(f"⚠️ {prov} 无可用模型")
                        return
                    model_ids = sorted(set(m.get("id", "") for m in model_list if m.get("id")))
                    if not hasattr(self, "_model_cache"):
                        self._model_cache = {}
                    self._model_cache[prov] = model_ids

                # Show prefix filters
                prefixes: dict[str, int] = {}
                for mid in model_ids:
                    pfx = mid.split("/")[0] if "/" in mid else "other"
                    prefixes[pfx] = prefixes.get(pfx, 0) + 1
                sorted_pfx = sorted(prefixes.items(), key=lambda x: -x[1])
                lines = [f"📋 {prov} 可用模型 ({len(model_ids)}):\n", "选择厂商前缀筛选:"]
                buttons = []
                for pfx, cnt in sorted_pfx[:20]:
                    buttons.append([InlineKeyboardButton(f"📂 {pfx} ({cnt})", callback_data=f"ml_pfx:{prov}:{pfx}")])
                buttons.append([InlineKeyboardButton("🔍 搜索模型", callback_data=f"ml_srch:{prov}")])
                buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")])
                await query.edit_message_text(
                    "\n".join(lines)[:4000],
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            elif data.startswith("ml_pfx:"):
                # ml_pfx:provider:prefix or ml_pfx:provider:prefix:page
                parts = data.split(":")
                if len(parts) < 3:
                    return
                prov, prefix = parts[1], parts[2]
                page = int(parts[3]) if len(parts) > 3 else 0
                per_page = 15
                cache = getattr(self, "_model_cache", {})
                model_ids = cache.get(prov, [])
                filtered = [m for m in model_ids if m.startswith(f"{prefix}/") or (prefix == "other" and "/" not in m)]
                pm = self._load_pm()
                existing = set(pm.get("models", {}).get(prov, []))

                filtered = self._sort_models_newest_first(filtered)
                total_pages = max(1, (len(filtered) + per_page - 1) // per_page)
                page = min(page, total_pages - 1)
                start = page * per_page
                page_items = filtered[start:start + per_page]

                lines = [f"📋 {prov} / {prefix} ({len(filtered)}) [第{page+1}/{total_pages}页]:\n"]
                for mid in page_items:
                    if mid in existing:
                        lines.append(f"  ✅ {mid}")
                    else:
                        lines.append(f"  ⚪️ {mid}")
                buttons = self._build_model_buttons_2col(
                    page_items, prov, existing, strip_prefix=prefix,
                )
                # Navigation
                nav = []
                if page > 0:
                    nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"ml_pfx:{prov}:{prefix}:{page-1}"))
                if page < total_pages - 1:
                    nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"ml_pfx:{prov}:{prefix}:{page+1}"))
                if nav:
                    buttons.append(nav)
                buttons.append([InlineKeyboardButton("⬅️ 返回厂商列表", callback_data=f"ep_models:{prov}")])
                await query.edit_message_text(
                    "\n".join(lines)[:4000],
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            elif data.startswith("ml_srch:"):
                # ml_srch:provider — prompt user to type search keyword
                prov = data[8:]
                chat_id = str(query.message.chat_id)
                self._edit_state[chat_id] = {"action": "model_search", "provider": prov}
                await query.edit_message_text(f"🔍 搜索 {prov} 模型\n\n请输入关键词 (如 claude, llama, qwen):")

            elif data.startswith("ep_addm:"):
                # ep_addm:provider:model_id — add model to provider
                parts = data.split(":", 2)
                if len(parts) < 3:
                    return
                prov, model_id = parts[1], parts[2]
                pm = self._load_pm()
                if prov not in pm.get("providers", {}):
                    await query.edit_message_text(f"❌ 提供商 {prov} 不存在")
                    return
                models = pm.setdefault("models", {})
                prov_models = models.setdefault(prov, [])
                if model_id in prov_models:
                    await query.edit_message_text(f"⚠️ {model_id} 已存在")
                    return
                prov_models.append(model_id)
                self._save_pm(pm)
                # Reload in provider
                if self._groupchat_engine:
                    self._groupchat_engine.provider._pm_overrides = None
                # Toast notification + refresh list locally
                try:
                    await query.answer(f"✅ 已添加 {model_id}", show_alert=False)
                except Exception:
                    pass
                # Rebuild model list from saved pm (no API re-fetch)
                existing = set(prov_models)
                all_models = getattr(self, "_model_cache", {}).get(prov, [])
                if not all_models:
                    # No cache, just show confirmation
                    await query.edit_message_text(
                        f"✅ 已添加 {model_id} 到 {prov}\n"
                        f"当前 {len(prov_models)} 个模型\n\n"
                        f"用 /editagent 切换 agent 模型",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("📋 刷新列表", callback_data=f"ep_models:{prov}")],
                            [InlineKeyboardButton("⬅️ 返回", callback_data=f"ep_pick:{prov}")],
                        ])
                    )
                    return
                # Rebuild from cache — stay in same prefix filter
                prefix = model_id.split("/")[0] if "/" in model_id else "other"
                filtered = [m for m in all_models if m.startswith(f"{prefix}/") or (prefix == "other" and "/" not in m)]
                filtered = self._sort_models_newest_first(filtered)
                lines = [f"📋 {prov} / {prefix} ({len(filtered)}):\n"]
                page_items = filtered[:30]
                for mid in page_items:
                    if mid in existing:
                        lines.append(f"  ✅ {mid}")
                    else:
                        lines.append(f"  ⚪️ {mid}")
                buttons = self._build_model_buttons_2col(page_items, prov, existing, strip_prefix=prefix)
                if len(filtered) > 30:
                    lines.append(f"  ... 和 {len(filtered) - 30} 个更多")
                buttons.append([InlineKeyboardButton("⬅️ 返回厂商列表", callback_data=f"ep_models:{prov}")])
                await query.edit_message_text(
                    "\n".join(lines)[:4000],
                    reply_markup=InlineKeyboardMarkup(buttons),
                )

            # ── History settings callbacks ──
            elif data.startswith("hs_"):
                await self._handle_history_callback(query, data)

            # ── Think callbacks ──
            elif data.startswith("think_"):
                await self._handle_think_callback(query, data)

        except Exception as e:
            logger.exception("Callback error: data={}", data)
            try:
                await query.edit_message_text(
                    f"❌ 按钮处理出错: {e}",
                    parse_mode=None,
                )
            except Exception:
                pass

    # ── Parameter documentation for /history UI ────────────────────────────
    _PARAM_DOCS: dict[str, dict[str, str]] = {
        "__top__:context_window_tokens": {
            "label": "上下文窗口 (tokens)",
            "location": "全局 → 贯穿整个上下文管理链",
            "doc": (
                "LLM 单次请求可接收的最大 token 数。所有裁剪、"
                "合并策略的上限锚点。\n\n"
                "算法链路:\n"
                "  1. context_pruning 按此值的 30%/50% "
                "触发 soft/hard 裁剪\n"
                "  2. MemoryConsolidator 在达到 50% 时"
                "将旧消息合并为摘要\n"
                "  3. 最终发送给 LLM 的 prompt 不超过此值\n\n"
                "建议: 与你使用的模型窗口匹配 (如 GPT-4.1 → 1,000,000)"
            ),
        },
        "__top__:tool_result_max_chars": {
            "label": "工具结果截断上限 (字符)",
            "location": "全局 → agent loop 入口处",
            "doc": (
                "工具返回的原始输出在进入任何后处理之前的"
                "字符硬上限。超过即截断。\n\n"
                "位置: agent/loop.py 初始化时读取\n"
                "时机: 最早的一刀 — 在 Stage 1 分工具截断之前\n"
                "截断方式: 保留首尾各一半，中间标记 truncated\n\n"
                "建议: 应 ≥ 各工具 max_chars 中的最大值"
            ),
        },
        "tool_results:exec_max_chars": {
            "label": "exec 工具截断 (字符)",
            "location": "Stage 1 → 命令执行输出",
            "doc": (
                "shell 命令 (exec tool) 返回结果的最大字符数。\n\n"
                "场景: pip install、git log、ls -la 等命令\n"
                "截断方式: head(前半) + '(N chars truncated)' + tail(后半)\n"
                "下游关系: 截断后若仍超 summarize_threshold → "
                "进入 Stage 2 AI 总结\n\n"
                "重要: 若此值 ≤ summarize_threshold，"
                "则 AI 总结永远不会被触发 (先截断了)"
            ),
        },
        "tool_results:web_fetch_max_chars": {
            "label": "web_fetch 截断 (字符)",
            "location": "Stage 1 → 网页抓取输出",
            "doc": (
                "web_fetch 工具 (URL 抓取) 返回内容的最大字符数。\n\n"
                "场景: 抓取网页/API 的 HTML→Markdown 转换结果\n"
                "截断方式: head + truncated 标记 + tail\n"
                "特点: 网页内容通常含大量导航/页脚噪音，"
                "适当降低可提升信噪比\n\n"
                "建议: 一般 8,000-15,000 即可覆盖正文"
            ),
        },
        "tool_results:web_search_max_chars": {
            "label": "web_search 截断 (字符)",
            "location": "Stage 1 → 搜索结果输出",
            "doc": (
                "web_search 工具返回的搜索结果最大字符数。\n\n"
                "场景: 搜索引擎结果摘要列表\n"
                "截断方式: head + truncated 标记 + tail\n"
                "特点: 搜索结果结构化程度高、信息密度大，"
                "通常比网页内容更紧凑\n\n"
                "建议: 5,000-10,000 即可包含足够条目"
            ),
        },
        "tool_results:summarize_threshold": {
            "label": "AI 总结触发阈值 (字符)",
            "location": "Stage 2 → 总结器入口判断",
            "doc": (
                "工具输出超过此字符数时，调用小模型提取关键信息。\n\n"
                "流程: 原始输出 → LLM 提取要点 → 压缩结果注入上下文\n"
                "失败兜底: head+tail 截断 (summarizer.py)\n"
                "模型: 使用 summarize_model 指定的轻量模型\n\n"
                "关键约束: 必须 < exec/web_fetch/web_search_max_chars\n"
                "  否则输出在 Stage 1 已被截断到阈值以下，"
                "总结器永远不触发\n\n"
                "建议: 设为各工具截断值的 60-80%"
            ),
        },
        "tool_results:summarize_enabled": {
            "label": "AI 总结开关",
            "location": "Stage 2 → 总结器启用/禁用",
            "doc": (
                "控制是否启用 LLM 自动总结工具输出。\n\n"
                "开启: 超过阈值的工具结果用小模型压缩\n"
                "关闭: 跳过总结，仅依靠 Stage 1 截断"
            ),
        },
        "tool_results:summarize_model": {
            "label": "总结模型",
            "location": "Stage 2 → 总结用 LLM",
            "doc": (
                "用于压缩工具输出的轻量模型。通过 OpenRouter 调用。\n\n"
                "要求: 低延迟、低成本、能准确提取关键信息\n"
                "配置: 也可在 ~/.nanobot/agents/reader/config.json 覆盖"
            ),
        },
        "tool_results:summarize_max_input_chars": {
            "label": "总结器最大输入 (字符)",
            "location": "Stage 2 → 总结器调用",
            "doc": (
                "发送给总结模型的最大输入字符数。\n\n"
                "工具输出先按此长度截断，再发给小模型提取要点。\n"
                "过小会丢失尾部信息，过大会增加 nano 模型成本。\n\n"
                "建议: 与 summarize_threshold 保持一致或略大"
            ),
        },
        "tool_results:summarize_max_output_chars": {
            "label": "总结器最大输出 (tokens)",
            "location": "Stage 2 → 总结器调用",
            "doc": (
                "总结模型生成摘要的最大 token 数 (max_tokens)。\n\n"
                "控制摘要的最大长度。过小可能截断关键信息，"
                "过大则摘要冗长、上下文膨胀。\n\n"
                "建议: 2000-6000"
            ),
        },
        "tool_results:broadcast_result_max_chars": {
            "label": "广播模式 result_max_chars",
            "location": "Stage 2 → broadcast tool_loop",
            "doc": (
                "广播模式下 tool_loop 的 result_max_chars 参数。\n\n"
                "控制每个工具结果注入 LLM 上下文前的最大字符数。\n"
                "超过此值会触发 AI 总结或截断。\n"
                "广播模式通常需要更大的值，因为多 agent 并行。\n\n"
                "建议: 15,000-30,000"
            ),
        },
        "tool_results:direct_result_max_chars": {
            "label": "直接模式 result_max_chars",
            "location": "Stage 2 → direct/serial tool_loop",
            "doc": (
                "直接对话/串行模式下 tool_loop 的 result_max_chars。\n\n"
                "控制每个工具结果注入 LLM 上下文前的最大字符数。\n"
                "超过此值会触发 AI 总结或截断。\n\n"
                "建议: 6,000-12,000"
            ),
        },
        "history:max_messages": {
            "label": "最大消息条数",
            "location": "Stage 3 → 历史窗口裁剪",
            "doc": (
                "对话历史中保留的最大消息数量。\n\n"
                "算法: 超过时从最早的消息开始丢弃，"
                "保证 assistant tool_call 与 tool result 配对完整\n"
                "位置: session/manager.py get_history()\n\n"
                "与 max_context_chars 的关系:\n"
                "  两个限制取先触发者 — 哪个先到就执行裁剪\n"
                "  若消息数很少但单条很长 → max_context_chars 先触发\n"
                "  若消息多但都很短 → max_messages 先触发\n\n"
                "建议: 根据平均消息长度调整，"
                "确保与 max_context_chars 匹配"
            ),
        },
        "history:max_context_chars": {
            "label": "最大上下文字符数",
            "location": "Stage 3 → 历史窗口裁剪",
            "doc": (
                "对话历史的总字符数上限。\n\n"
                "算法: sum(所有消息 content 长度)，超过时从最早丢弃\n"
                "位置: groupchat/prompt_builder.py 构建 prompt 时检查\n\n"
                "与 context_window_tokens 的关系:\n"
                "  此值是字符数，context_window 是 token 数\n"
                "  粗略换算: 1 token ≈ 4 字符 (英文) / 2 字符 (中文)\n"
                "  建议此值 ≤ context_window_tokens × 2\n\n"
                "与 max_messages 的关系: 两者取先触发"
            ),
        },
        "history:compress_ratio": {
            "label": "历史压缩触发比例",
            "location": "Stage 3 → 历史压缩",
            "doc": (
                "当消息数达到 max_messages × 此比例时，\n"
                "触发历史压缩（将最早一半消息用小模型摘要）。\n\n"
                "值域: 0.0-1.0，默认 0.8\n"
                "建议: 0.7-0.9"
            ),
        },
        "history:compress_max_summary_tokens": {
            "label": "历史压缩摘要长度 (tokens)",
            "location": "Stage 3 → 历史压缩",
            "doc": (
                "压缩历史时，摘要模型的最大输出 token 数。\n\n"
                "控制生成摘要的长度上限。\n"
                "建议: 400-800"
            ),
        },
        "context_pruning:soft_ratio": {
            "label": "软裁剪触发比例",
            "location": "Stage 4 → context_pruning",
            "doc": (
                "tool_loop 迭代 2+ 时，当上下文字符数超过\n"
                "context_window_tokens × CHARS_PER_TOKEN × 此比例\n"
                "时触发软裁剪。\n\n"
                "软裁剪: 旧 tool result 截断为 head+tail，"
                "中间部分提取关键事实。\n\n"
                "建议: 0.2-0.4"
            ),
        },
        "context_pruning:keep_recent": {
            "label": "保护最近 N 轮",
            "location": "Stage 4 → context_pruning",
            "doc": (
                "最近 N 个 assistant turn 的 tool result 不被裁剪。\n\n"
                "保护最近的工具结果，确保模型能引用最新数据。\n"
                "建议: 2-5"
            ),
        },
        "context_pruning:soft_max_chars": {
            "label": "软裁剪阈值 (字符)",
            "location": "Stage 4 → context_pruning",
            "doc": (
                "tool result 超过此长度才会被软裁剪。\n"
                "低于此值的 tool result 保持原样。\n\n"
                "建议: 3,000-6,000"
            ),
        },
        }

    async def _handle_history_callback(self, query, data: str) -> None:
        """Handle /history interactive settings callbacks."""
        from nanobot.groupchat.history import history_settings as hs
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if data == "hs_reload":
            hs.reload()
            await query.edit_message_text("🔄 配置已重载")
            return

        if data == "hs_global":
            settings = hs.get_all()
            d1 = self._PARAM_DOCS["__top__:context_window_tokens"]
            d2 = self._PARAM_DOCS["__top__:tool_result_max_chars"]
            text = (
                "🌐 全局设置\n"
                "影响整条上下文管理链的顶层参数\n\n"
                f"━ {d1['label']} ━\n"
                f"  当前值: {settings['context_window_tokens']:,}\n"
                f"  位置: {d1['location']}\n"
                f"  {d1['doc'].split(chr(10))[0]}\n\n"
                f"━ {d2['label']} ━\n"
                f"  当前值: {settings['tool_result_max_chars']:,}\n"
                f"  位置: {d2['location']}\n"
                f"  {d2['doc'].split(chr(10))[0]}\n"
            )
            buttons = [
                [InlineKeyboardButton(f"上下文窗口: {settings['context_window_tokens']:,}", callback_data="hs_edit:__top__:context_window_tokens")],
                [InlineKeyboardButton(f"工具结果截断: {settings['tool_result_max_chars']:,}", callback_data="hs_edit:__top__:tool_result_max_chars")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return

        if data == "hs_stage1":
            settings = hs.get_all()
            tr = settings["tool_results"]
            d_exec = self._PARAM_DOCS["tool_results:exec_max_chars"]
            d_web = self._PARAM_DOCS["tool_results:web_fetch_max_chars"]
            d_search = self._PARAM_DOCS["tool_results:web_search_max_chars"]
            text = (
                "📝 Stage 1: 工具输出截断\n"
                "工具返回长文本时，在源头按工具类型分别截断\n"
                "截断方式: 保留首尾各一半，中间标记 (N chars truncated)\n\n"
                f"━ {d_exec['label']} ━\n"
                f"  当前: {tr['exec_max_chars']:,} 字符\n"
                f"  {d_exec['doc'].split(chr(10))[0]}\n\n"
                f"━ {d_web['label']} ━\n"
                f"  当前: {tr['web_fetch_max_chars']:,} 字符\n"
                f"  {d_web['doc'].split(chr(10))[0]}\n\n"
                f"━ {d_search['label']} ━\n"
                f"  当前: {tr['web_search_max_chars']:,} 字符\n"
                f"  {d_search['doc'].split(chr(10))[0]}\n"
            )
            buttons = [
                [InlineKeyboardButton(f"exec: {tr['exec_max_chars']:,}", callback_data="hs_edit:tool_results:exec_max_chars")],
                [InlineKeyboardButton(f"web_fetch: {tr['web_fetch_max_chars']:,}", callback_data="hs_edit:tool_results:web_fetch_max_chars")],
                [InlineKeyboardButton(f"web_search: {tr['web_search_max_chars']:,}", callback_data="hs_edit:tool_results:web_search_max_chars")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_stage2":
            settings = hs.get_all()
            tr = settings["tool_results"]
            enabled = tr["summarize_enabled"]
            toggle_text = "❌ 关闭" if enabled else "✅ 开启"
            toggle_val = "false" if enabled else "true"
            d_thresh = self._PARAM_DOCS["tool_results:summarize_threshold"]
            d_model = self._PARAM_DOCS["tool_results:summarize_model"]
            text = (
                "🤖 Stage 2: AI 总结压缩\n"
                "工具结果超过阈值时，用小模型提取关键信息\n\n"
                f"  状态 → {'✅ 开启' if enabled else '❌ 关闭'}\n\n"
                f"━ {d_thresh['label']} ━\n"
                f"  当前: {tr['summarize_threshold']:,} 字符\n"
                f"  位置: {d_thresh['location']}\n"
                f"  {d_thresh['doc'].split(chr(10))[0]}\n\n"
                f"━ {d_model['label']} ━\n"
                f"  当前: {tr['summarize_model']}\n"
                f"  {d_model['doc'].split(chr(10))[0]}\n\n"
                "流程: raw → LLM提取要点 → 压缩注入上下文\n"
                "兜底: LLM 失败时降级为 head+tail 截断"
            )
            buttons = [
                [InlineKeyboardButton(f"{toggle_text} AI总结", callback_data=f"hs_set:tool_results:summarize_enabled:{toggle_val}")],
                [InlineKeyboardButton(f"阈值: {tr['summarize_threshold']:,}", callback_data="hs_edit:tool_results:summarize_threshold")],
                [InlineKeyboardButton(f"最大输入: {tr.get('summarize_max_input_chars', 8000):,}", callback_data="hs_edit:tool_results:summarize_max_input_chars")],
                [InlineKeyboardButton(f"最大输出: {tr.get('summarize_max_output_chars', 4000):,}", callback_data="hs_edit:tool_results:summarize_max_output_chars")],
                [InlineKeyboardButton(f"广播模式: {tr.get('broadcast_result_max_chars', 20000):,}", callback_data="hs_edit:tool_results:broadcast_result_max_chars")],
                [InlineKeyboardButton(f"直接模式: {tr.get('direct_result_max_chars', 8000):,}", callback_data="hs_edit:tool_results:direct_result_max_chars")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_stage3":
            settings = hs.get_all()
            hist = settings["history"]
            engine = self._groupchat_engine
            current_msgs = len(engine._history) if engine else 0
            current_chars = sum(len(m.get("content", "")) for m in (engine._history if engine else []))
            d_msgs = self._PARAM_DOCS["history:max_messages"]
            d_chars = self._PARAM_DOCS["history:max_context_chars"]
            text = (
                "📚 Stage 3: 历史存储\n"
                "对话历史超过限制时，从最早消息开始丢弃\n\n"
                f"━ {d_msgs['label']} ━\n"
                f"  当前: {hist['max_messages']} 条\n"
                f"  {d_msgs['doc'].split(chr(10))[0]}\n\n"
                f"━ {d_chars['label']} ━\n"
                f"  当前: {hist['max_context_chars']:,} 字符\n"
                f"  {d_chars['doc'].split(chr(10))[0]}\n\n"
                f"  实时状态 → {current_msgs} 条 / {current_chars:,} 字符"
            )
            buttons = [
                [InlineKeyboardButton(f"消息数: {hist['max_messages']}", callback_data="hs_edit:history:max_messages")],
                [InlineKeyboardButton(f"上下文: {hist['max_context_chars']:,}", callback_data="hs_edit:history:max_context_chars")],
                [InlineKeyboardButton(f"压缩比例: {hist.get('compress_ratio', 0.8)}", callback_data="hs_edit:history:compress_ratio")],
                [InlineKeyboardButton(f"摘要tokens: {hist.get('compress_max_summary_tokens', 600)}", callback_data="hs_edit:history:compress_max_summary_tokens")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_stage4":
            settings = hs.get_all()
            cp = settings.get("context_pruning", {})
            text = (
                "✂️ Stage 4: 迭代上下文裁剪\n"
                "tool_loop 迭代 2+ 时，自动裁剪旧 tool result\n\n"
                f"  软裁剪比例 → {cp.get('soft_ratio', 0.3)}\n"
                f"  保护最近   → {cp.get('keep_recent', 3)} 轮\n"
                f"  软裁剪阈值 → {cp.get('soft_max_chars', 4000):,} 字符"
            )
            buttons = [
                [InlineKeyboardButton(f"软裁剪比例: {cp.get('soft_ratio', 0.3)}", callback_data="hs_edit:context_pruning:soft_ratio")],
                [InlineKeyboardButton(f"保护最近: {cp.get('keep_recent', 3)}", callback_data="hs_edit:context_pruning:keep_recent")],
                [InlineKeyboardButton(f"软裁剪阈值: {cp.get('soft_max_chars', 4000):,}", callback_data="hs_edit:context_pruning:soft_max_chars")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_back":
            # Rebuild main /history view
            settings = hs.get_all()
            tr = settings["tool_results"]
            hist = settings["history"]
            cp = settings.get("context_pruning", {})
            engine = self._groupchat_engine
            current_msgs = len(engine._history) if engine else 0
            current_chars = sum(len(m.get("content", "")) for m in (engine._history if engine else []))
            compress_trigger = int(hist["max_messages"] * hist.get("compress_ratio", 0.8))
            ctx_chars_limit = settings["context_window_tokens"] * 4
            ai_on = tr["summarize_enabled"]
            prune_soft_budget = int(ctx_chars_limit * cp.get("soft_ratio", 0.3))
            text = (
                "─── 上下文管线 · 实时演示 ───\n"
                f"全局: context_window={settings['context_window_tokens']:,} tokens"
                f" | tool_result_max={settings['tool_result_max_chars']:,} 字符\n"
                f"历史: max_messages={hist['max_messages']}条"
                f" | max_context_chars={hist['max_context_chars']:,}\n"
                f"当前: {current_msgs}条 / {current_chars:,}字符\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "\n"
                "👤 用户: 帮我搜索特朗普的图片并下载\n"
                f" └─ 🛡 [头部永久保护] 此消息不压缩不截断\n"
                "\n"
                "── 轮次 1 ──\n"
                "🤖 Agent: 我来搜索… → web_search(query='特朗普图片')\n"
                "\n"
                f"📡 web_search 返回 12,000 字符:\n"
                f" └─ [截断①] web_search_max_chars={tr['web_search_max_chars']:,}\n"
                f"    原始 12,000 > {tr['web_search_max_chars']:,} → 仅保留前后各一半\n"
                f" └─ [截断②] tool_result_max_chars={settings['tool_result_max_chars']:,}\n"
                f"    全局硬上限，截断后结果不超此值\n"
                f" └─ [AI压缩] summarize_enabled={'✅' if ai_on else '❌'}"
                f" | 触发阈值=>{tr['summarize_threshold']:,}字符\n"
                f"    {'✅ 触发: ' if ai_on else '🚫 未触发(已关闭): '}"
                f"model={tr['summarize_model']}"
                f" | 最大输入={tr.get('summarize_max_input_chars', 8000):,}"
                f" | 最大输出={tr.get('summarize_max_output_chars', 4000):,}tokens\n"
                f"    → 摘要注入上下文(广播模式最大={tr.get('broadcast_result_max_chars', 20000):,}"
                f" | 直接模式最大={tr.get('direct_result_max_chars', 8000):,})\n"
                "\n"
                "── 轮次 2 ──\n"
                "🤖 Agent: 现在执行下载 → exec(python send_photo.py)\n"
                "\n"
                f"💻 exec 返回报错 3,000 字符:\n"
                f" └─ [截断①] exec_max_chars={tr['exec_max_chars']:,}\n"
                f"    3,000 < {tr['exec_max_chars']:,} → 未触发截断，完整保留\n"
                f" └─ [AI压缩] 3,000 < {tr['summarize_threshold']:,} → 未触发AI压缩\n"
                "\n"
                f"── [上下文裁剪] tool_loop 第2次迭代起自动检查 ──\n"
                f" 软裁剪: 上下文>{prune_soft_budget:,}字符({cp.get('soft_ratio',0.3)}×窗口)\n"
                f"   → 对超过soft_max_chars={cp.get('soft_max_chars',4000):,}的旧工具结果\n"
                f"      替换为精简摘要(仅保留路径/错误/kv)\n"
                f" 保护: 最近{cp.get('keep_recent',3)}轮的工具结果不裁剪\n"
                "\n"
                f"── [历史记忆压缩] 消息数>={compress_trigger}条触发 ──\n"
                f"   compress_ratio={hist.get('compress_ratio',0.8)} × max_messages={hist['max_messages']}"
                f" = {compress_trigger}条时触发\n"
                f"   🛡 头部保护: 首条消息+首条用户消息 → 永不压缩\n"
                f"   🗜 压缩中间段 → model={tr['summarize_model']}"
                f" | max_tokens={hist.get('compress_max_summary_tokens',600)}\n"
                f"   🛡 尾部保护: 最近6轮完整保留\n"
                "\n"
                "── 超过总限制时 ──\n"
                f"   max_messages={hist['max_messages']}条 或 max_context_chars={hist['max_context_chars']:,}字符\n"
                f"   → 从最早消息开始丢弃(tool_call与result配对一起丢)\n"
                f"\n📊 当前: {current_msgs}/{hist['max_messages']}条"
                f" | {current_chars:,}/{hist['max_context_chars']:,}字符\n"
            )
            buttons = [
                [
                    InlineKeyboardButton(
                        f"🌐 全局: ctx={settings['context_window_tokens']:,}tok / max_result={settings['tool_result_max_chars']:,}",
                        callback_data="hs_global",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"✂️ 工具截断: exec={tr['exec_max_chars']:,} fetch={tr['web_fetch_max_chars']:,} search={tr['web_search_max_chars']:,}",
                        callback_data="hs_stage1",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🧠 AI压缩: {'✅' if ai_on else '❌'} 阈值={tr['summarize_threshold']:,} 模型={tr['summarize_model'].split('/')[-1]}",
                        callback_data="hs_stage2",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"📚 历史: max={hist['max_messages']}条/{hist['max_context_chars']:,}字 压缩@{compress_trigger}条",
                        callback_data="hs_stage3",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🔪 迭代裁剪: soft@{cp.get('soft_ratio',0.3)} 保留最近{cp.get('keep_recent',3)}轮",
                        callback_data="hs_stage4",
                    )
                ],
                [
                    InlineKeyboardButton("🔄 重载配置", callback_data="hs_reload"),
                ],
            ]
            await query.edit_message_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("hs_set:"):
            # Direct toggle: hs_set:section:key:value
            parts = data.split(":", 3)
            if len(parts) == 4:
                section, key, raw_val = parts[1], parts[2], parts[3]
                # Parse value
                if raw_val in ("true", "false"):
                    value = raw_val == "true"
                elif raw_val.isdigit():
                    value = int(raw_val)
                else:
                    value = raw_val
                result = hs.update_field(section, key, value)
                await query.answer(result, show_alert=True)
                # Refresh the stage view that owns this setting
                if section == "tool_results" and ("summarize" in key or "result_max_chars" in key):
                    await self._handle_history_callback(query, "hs_stage2")
                
                elif section == "context_pruning":
                    await self._handle_history_callback(query, "hs_stage4")
                elif section == "history":
                    await self._handle_history_callback(query, "hs_stage3")
                else:
                    await self._handle_history_callback(query, "hs_back")

        elif data.startswith("hs_edit:"):
            # Prompt user for new value: hs_edit:section:key
            parts = data.split(":", 2)
            if len(parts) == 3:
                section, key = parts[1], parts[2]
                if section == "__top__":
                    current = hs.get_all().get(key, "?")
                else:
                    current = hs.get_all().get(section, {}).get(key, "?")
                chat_id = str(query.message.chat_id)
                self._edit_state[chat_id] = {
                    "action": "history_setting",
                    "section": section,
                    "key": key,
                }
                # Build rich edit prompt with parameter documentation
                doc_key = f"{section}:{key}"
                param_doc = self._PARAM_DOCS.get(doc_key)
                current_display = f"{current:,}" if isinstance(current, int) else str(current)
                if param_doc:
                    text = (
                        f"✏️ 修改: {param_doc['label']}\n\n"
                        f"📍 位置: {param_doc['location']}\n\n"
                        f"📖 说明:\n{param_doc['doc']}\n\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"当前值: {current_display}\n"
                        f"请输入新值:"
                    )
                else:
                    label = key if section == "__top__" else f"{section}.{key}"
                    text = (
                        f"✏️ 修改 {label}\n\n"
                        f"当前值: {current_display}\n\n"
                        f"请输入新值:"
                    )
                await query.edit_message_text(text)

    async def _handle_edit_input(self, chat_id: str, content: str) -> None:
        """Process interactive edit state input."""
        state = self._edit_state[chat_id]
        logger.debug("Edit input: chat_id={} field={} content={}...", chat_id, state.get("field", "?"), content[:50])

        # History setting value input
        if state.get("action") == "history_setting":
            del self._edit_state[chat_id]
            section = state["section"]
            key = state["key"]
            raw = content.strip()
            # Detect type: float keys (ratios), string keys (model), else int
            _float_keys = {"soft_ratio", "compress_ratio"}
            _string_keys = {"summarize_model"}
            if key in _string_keys:
                value: Any = raw
            elif key in _float_keys:
                try:
                    value = float(raw)
                except ValueError:
                    await self._gc_send(chat_id, f"❌ 请输入数字，收到: {raw}")
                    return
            else:
                try:
                    value = int(raw)
                except ValueError:
                    await self._gc_send(chat_id, f"❌ 请输入数字，收到: {raw}")
                    return
            from nanobot.groupchat.history import history_settings as hs
            result = hs.update_field(section, key, value)
            await self._gc_send(chat_id, result)
            return

        # Model search handler
        if state.get("action") == "model_search":
            del self._edit_state[chat_id]
            prov = state["provider"]
            keyword = content.strip().lower()
            cache = getattr(self, "_model_cache", {})
            model_ids = cache.get(prov, [])
            filtered = [m for m in model_ids if keyword in m.lower()]

            pm = self._load_pm()
            existing = set(pm.get("models", {}).get(prov, []))
            filtered = self._sort_models_newest_first(filtered)
            lines = [f"🔍 搜索 \"{content.strip()}\" ({len(filtered)} 结果):\n"]
            page_items = filtered[:25]
            for mid in page_items:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪️ {mid}")
            buttons = self._build_model_buttons_2col(page_items, prov, existing)
            if len(filtered) > 25:
                lines.append(f"  ... 和 {len(filtered) - 25} 个更多")
            if not filtered:
                lines.append("  无匹配结果")
            buttons.append([InlineKeyboardButton("🔍 重新搜索", callback_data=f"ml_srch:{prov}")])
            buttons.append([InlineKeyboardButton("⬅️ 返回厂商列表", callback_data=f"ep_models:{prov}")])
            await self._app.bot.send_message(
                chat_id=int(chat_id),
                text="\n".join(lines)[:4000],
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        # Handle prompt_edit (from /prompt → edit component)
        if state.get("field") == "prompt_edit":
            del self._edit_state[chat_id]
            agent_name = state.get("agent", "")
            key = state.get("key", "")
            engine = self._groupchat_engine
            if engine:
                try:
                    result = engine.prompt_builder.update_prompt_component(
                        agent_name, key, content.strip(),
                        engine.registry, engine.workspace,
                        Path(engine.config.agents_dir or "~/.nanobot/agents").expanduser(),
                    )
                    # Send confirmation + refreshed component list
                    preview = (content.strip()[:200] + "…") if len(content.strip()) > 200 else content.strip()
                    await self._gc_send(chat_id, f"{result}\n\n📄 内容预览:\n{preview}")
                    text, markup = self._build_prompt_order_view(engine)
                    await self._app.bot.send_message(
                        chat_id=int(chat_id), text=text[:4096], reply_markup=markup,
                    )
                except Exception as e:
                    logger.error("prompt_edit failed: {} key={} err={}", agent_name, key, e)
                    await self._gc_send(chat_id, f"❌ 编辑失败: {e}")
            return

        # Handle custom prompt component name input
        if state.get("field") == "pradd_custom_name":
            del self._edit_state[chat_id]
            name_input = content.strip()
            if not name_input or name_input in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            # Sanitize: use the input as label, derive a key from it
            import re
            key = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff]', '_', name_input).strip('_').lower()
            if not key:
                key = f"custom_{hash(name_input) % 10000}"
            label = f"{name_input} ({key})"
            engine = self._groupchat_engine
            if engine:
                result = PromptBuilder.add_custom_component(key, label)
                if result.startswith("✅"):
                    # Also add to prompt order
                    order = engine.prompt_builder.get_agent_prompt_order()
                    if key not in order:
                        order.append(key)
                        engine.prompt_builder.set_default_prompt_order(order)
                await self._gc_send(chat_id, result)
                # Show refreshed component list with edit buttons
                text, markup = self._build_prompt_order_view(engine)
                await self._app.bot.send_message(
                    chat_id=int(chat_id), text=text[:4096], reply_markup=markup,
                )
            return

        field = state["field"]

        # Handle hyperparams value input
        if field == "hp_value":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            hp_key = state.get("hp_key", "")
            raw_val = content.strip()
            if hp_key in ("reasoning_effort", "stop"):
                value = None if raw_val.lower() in ("off", "none", "null") else raw_val
            else:
                try:
                    value = float(raw_val)
                except ValueError:
                    await self._gc_send(chat_id, "⚠️ 值必须是数字")
                    return
            provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
            params = getattr(provider, 'sampling_params', None) if provider else None
            if params is not None:
                old_val = params.get(hp_key, None)
                params[hp_key] = value
                # Persist to disk
                hp_path = Path.home() / ".nanobot" / "hyperparams.json"
                try:
                    hp_path.write_text(json.dumps(params, indent=2))
                    logger.info("Persisted hyperparams (set {}={}) to {}", hp_key, value, hp_path)
                except Exception as e:
                    logger.error("Failed to persist hyperparams: {}", e)
                    await self._gc_send(chat_id, f"⚠️ 参数已生效但持久化失败: {e}")
                if old_val is not None:
                    await self._gc_send(chat_id, f"✅ {hp_key}: {old_val} → {value}\n即时生效，已持久化")
                else:
                    await self._gc_send(chat_id, f"✅ 已添加 {hp_key} = {value}\n即时生效，已持久化")
                # Refresh keyboard
                await self._send_hyperparams_keyboard(chat_id, params)
            return

        # Handle custom hyperparam name input
        if field == "hp_add_custom":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            key = content.strip().lower().replace(" ", "_")
            self._edit_state[chat_id] = {"field": "hp_value", "hp_key": key, "hp_is_new": True}
            await self._gc_send(chat_id, f"➕ 添加 {key}\n\n请输入值 (数字):")
            return

        # Handle agent hyperparams value input
        if field == "ahp_value":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            hp_key = state.get("hp_key", "")
            a_name = state.get("agent", "")
            raw_val = content.strip()
            if hp_key in ("reasoning_effort", "stop"):
                value = None if raw_val.lower() in ("off", "none", "null") else raw_val
            else:
                try:
                    value = float(raw_val)
                except ValueError:
                    await self._gc_send(chat_id, "⚠️ 值必须是数字")
                    return
            if self._groupchat_engine and a_name in self._groupchat_engine.registry:
                agent = self._groupchat_engine.registry[a_name]
                if "hyperparams" not in agent or not isinstance(agent["hyperparams"], dict):
                    agent["hyperparams"] = {}
                old_val = agent["hyperparams"].get(hp_key)
                agent["hyperparams"][hp_key] = value
                # Persist to config.json
                cfg_path = Path.home() / ".nanobot" / "agents" / a_name.lower() / "config.json"
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text())
                        cfg.setdefault("hyperparams", {})
                        cfg["hyperparams"][hp_key] = value
                        cfg_path.write_text(json.dumps(cfg, indent=2))
                    except Exception as e:
                        logger.error("Failed to save agent hyperparams: {}", e)
                        await self._gc_send(chat_id, f"⚠️ 参数已生效但持久化失败: {e}")
                if old_val is not None:
                    await self._gc_send(chat_id, f"✅ {a_name} {hp_key}: {old_val} → {value}")
                else:
                    await self._gc_send(chat_id, f"✅ {a_name} 已添加 {hp_key} = {value}")
                await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent["hyperparams"])
            return

        # Handle agent custom hyperparam name input
        if field == "ahp_add_custom":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            a_name = state.get("agent", "")
            key = content.strip().lower().replace(" ", "_")
            self._edit_state[chat_id] = {"field": "ahp_value", "agent": a_name, "hp_key": key, "hp_is_new": True}
            await self._gc_send(chat_id, f"➕ 为 {a_name} 添加 {key}\n\n请输入值 (数字):")
            return

        # Handle groupchat settings value input
        if field == "gc_value":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            gc_key = state.get("gc_key", "")
            try:
                value = int(content.strip())
            except ValueError:
                await self._gc_send(chat_id, "⚠️ 值必须是整数")
                return
            if value < 1:
                await self._gc_send(chat_id, "⚠️ 值必须 ≥ 1")
                return
            settings = self._load_gc_settings()
            old_val = settings.get(gc_key, self.GC_SETTINGS_DEFAULTS.get(gc_key))
            settings[gc_key] = value
            self._save_gc_settings(settings)
            label = self.GC_SETTINGS_LABELS.get(gc_key, gc_key)
            await self._gc_send(chat_id, f"✅ {label}: {old_val} → {value}\n下次群聊生效，已持久化")
            return
        if field == "sg_name":
            del self._edit_state[chat_id]
            if content.strip() in ("0", "取消", "/cancel"):
                await self._gc_send(chat_id, "❌ 已取消")
                return
            result = self._groupchat_engine.save_group(content.strip())
            await self._gc_send(chat_id, result)
            return

        # Universal cancel — works at any edit prompt
        if content.strip() in ("0", "取消", "/cancel"):
            del self._edit_state[chat_id]
            await self._gc_send(chat_id, "❌ 已取消")
            return

        # Handle provider/model management flows
        if state.get("mode") == "pm":
            field = state["field"]
            if field == "pm_prov_name":
                name = content.strip().lower()
                state["prov_name"] = name
                state["field"] = "pm_prov_url"
                await self._gc_send(chat_id, f"提供商: {name}\n\n请输入 API Base URL\n(如 https://openrouter.ai/v1):")
                return
            elif field == "pm_prov_url":
                url = content.strip().rstrip("/")
                state["prov_url"] = url
                state["field"] = "pm_prov_key"
                await self._gc_send(chat_id, f"🔗 URL: {url}\n\n请输入 API Key:")
                return
            elif field == "pm_prov_key":
                api_key = content.strip()
                name = state["prov_name"]
                url = state["prov_url"]
                pm = self._load_pm()
                pm.setdefault("providers", {})[name] = {"url": url, "apiKey": api_key}
                pm.setdefault("models", {}).setdefault(name, [])
                self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ 提供商 {name} 已创建!\n🔗 {url}\n🔑 {api_key[:8]}...")
                return
            elif field == "pm_model_id":
                model_id = content.strip()
                prov = state["provider"]
                pm = self._load_pm()
                pm.setdefault("models", {}).setdefault(prov, [])
                if model_id not in pm["models"][prov]:
                    pm["models"][prov].append(model_id)
                self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ 模型已添加!\n🏢 {prov} / 🤖 {model_id}")
                return
            elif field == "ep_url":
                prov = state["prov_name"]
                pm = self._load_pm()
                if prov in pm.get("providers", {}):
                    pm["providers"][prov]["url"] = content.strip().rstrip("/")
                    self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ {prov} URL 已更新: {content.strip()}")
                return
            elif field == "ep_key":
                prov = state["prov_name"]
                pm = self._load_pm()
                if prov in pm.get("providers", {}):
                    pm["providers"][prov]["apiKey"] = content.strip()
                    self._save_pm(pm)
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, f"✅ {prov} API Key 已更新: {content.strip()[:8]}...")
                return

        agent_name = state.get("agent", "")
        engine = self._groupchat_engine
        if not engine:
            del self._edit_state[chat_id]
            return



        # Handle /newagent create flow
        if state.get("mode") == "create":
            if field == "create_name":
                name = content.strip()
                if name in ("0", "取消"):
                    del self._edit_state[chat_id]
                    await self._gc_send(chat_id, "❌ 已取消")
                    return
                if engine._resolve_agent_name(name):
                    await self._gc_send(chat_id, f"⚠️ '{name}' 已存在，请换个名字:")
                    return
                state["agent"] = name
                state["field"] = "create_model"
                await self._gc_send(chat_id,
                    f"Agent: {name}\n\n请输入模型名:\n"
                    "(如 anthropic/claude-sonnet-4-5, x-ai/grok-4.1-fast)"
                )
                return
            if field == "create_model":
                model_name = content.strip()
                if model_name in ("0", "取消"):
                    del self._edit_state[chat_id]
                    await self._gc_send(chat_id, "❌ 已取消")
                    return
                await self._gc_send(chat_id, f"🔍 测试模型 {model_name}...")
                try:
                    response = await engine.provider.chat(
                        messages=[{"role": "user", "content": "Say 'hello' in one word."}],
                        model=model_name,
                        max_tokens=20,
                    )
                    reply = (response.content or "").strip()
                    state["model"] = model_name
                    state["field"] = "create_persona"
                    await self._gc_send(chat_id,
                        f"✅ 模型 {model_name} 可用!\n"
                        f"测试回复: {reply}\n\n"
                        f"请输入人设 (SOUL.md 内容):"
                    )
                except Exception as e:
                    await self._gc_send(chat_id,
                        f"❌ 模型 {model_name} 不可用: {e}\n\n"
                        f"请重新输入模型名，或发 0 取消:"
                    )
                return
            elif field == "create_persona":
                name = agent_name
                model = state["model"]
                prompt = content
                global_hp = getattr(engine.provider, 'sampling_params', None)
                agent_hp = dict(global_hp) if global_hp else {}
                engine.registry[name] = {"model": model, "prompt": prompt, "hyperparams": agent_hp}
                # Save to disk
                from pathlib import Path as _P
                soul_dir = _P.home() / ".nanobot" / "agents" / name.lower() / "workspace"
                soul_dir.mkdir(parents=True, exist_ok=True)
                (soul_dir / "SOUL.md").write_text(prompt)
                config_path = soul_dir.parent / "config.json"
                config_data = {"model": model, "rank": "basic"}
                if agent_hp:
                    config_data["hyperparams"] = agent_hp
                config_path.write_text(json.dumps(config_data, indent=2))
                preview = prompt[:80] + "..." if len(prompt) > 80 else prompt
                await self._gc_send(chat_id, f"✅ Agent {name} 已创建!\n模型: {model}\n人设: {preview}")
                del self._edit_state[chat_id]
                return

        if field is None:
            c = content.strip()
            if c in ("0", "取消"):
                del self._edit_state[chat_id]
                await self._gc_send(chat_id, "❌ 已取消")
                return
            field_map = {"1": "name", "2": "persona", "3": "model"}
            if c in field_map:
                state["field"] = field_map[c]
                prompts = {"name": "新名字", "persona": "新人设内容", "model": "新模型名 (如 anthropic/claude-sonnet-4-5)"}
                await self._gc_send(chat_id, f"请输入{prompts[field_map[c]]}:")
            else:
                await self._gc_send(chat_id, "请输入 1/2/3 或 0 取消")
            return

        if field == "name":
            new_name = content.strip()
            if new_name and new_name != agent_name:
                data = engine.registry.pop(agent_name)
                engine.registry[new_name] = data
                if agent_name in engine._active_agents:
                    idx = engine._active_agents.index(agent_name)
                    engine._active_agents[idx] = new_name
                # Update leader if needed
                if engine._leader == agent_name:
                    engine._leader = new_name
                    engine._state.save_leader(new_name)
                # Update saved groups
                groups = engine._state.load_groups()
                changed = False
                for gname, members in groups.items():
                    if agent_name in members:
                        groups[gname] = [new_name if m == agent_name else m for m in members]
                        changed = True
                if changed:
                    engine._state.save_groups(groups)
                # Rename directory
                from pathlib import Path as _P
                agents_dir = _P.home() / ".nanobot" / "agents"
                old_dir = agents_dir / agent_name.lower()
                new_dir = agents_dir / new_name.lower()
                if old_dir.exists() and not new_dir.exists():
                    old_dir.rename(new_dir)
                engine._state.save_active(engine._active_agents)
                await self._gc_send(chat_id, f"✅ {agent_name} → {new_name}")
            else:
                await self._gc_send(chat_id, "⚠️ 名字未变")
        elif field == "persona":
            engine.registry[agent_name]["prompt"] = content
            from pathlib import Path as _P
            soul_dir = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "workspace"
            soul_dir.mkdir(parents=True, exist_ok=True)
            (soul_dir / "SOUL.md").write_text(content)
            preview = content[:80] + "..." if len(content) > 80 else content
            await self._gc_send(chat_id, f"✅ {agent_name} 人设已更新:\n{preview}")
        elif field == "model":
            new_model = content.strip()
            engine.registry[agent_name]["model"] = new_model
            # Persist to disk
            from pathlib import Path as _P
            agent_entry = engine.registry[agent_name]
            if agent_entry.get("_default"):
                main_cfg_path = _P.home() / ".nanobot" / "config.json"
                if main_cfg_path.exists():
                    cfg = json.loads(main_cfg_path.read_text())
                    cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = new_model
                    main_cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            else:
                cfg_path = _P.home() / ".nanobot" / "agents" / agent_name.lower() / "config.json"
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text())
                    cfg["model"] = new_model
                    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
            await self._gc_send(chat_id, f"✅ {agent_name} 模型: {new_model}")

        del self._edit_state[chat_id]

    async def _handle_think_callback(self, query, data: str) -> None:
        """Handle /think interactive button callbacks."""
        engine = self._groupchat_engine
        if not engine:
            await query.edit_message_text("⚠️ 未配置群聊引擎")
            return

        if data.startswith("think_agent:"):
            # Show level-selection buttons for the chosen agent (or __all__)
            target = data[len("think_agent:"):]
            label = "全部 Agent" if target == "__all__" else target

            effort_options = [
                ("❌ 关闭", "off"),
                ("🔄 自动", "auto"),
                ("🔅 低", "low"),
                ("🔆 中", "medium"),
                ("✨ 高", "high"),
            ]
            buttons = [
                [InlineKeyboardButton(lbl, callback_data=f"think_set:{target}:{lvl}")]
                for lbl, lvl in effort_options
            ]
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="think_back")])
            await query.edit_message_text(
                f"🧠 设置思考强度 — {label}",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data.startswith("think_set:"):
            # Apply the setting: think_set:<name|__all__>:<level>
            parts = data.split(":", 2)
            if len(parts) != 3:
                return
            target, level_raw = parts[1], parts[2]

            effort: str | None = None if level_raw in ("off", "auto") else level_raw

            if target == "__all__":
                targets = list(engine.registry.keys())
            else:
                matched = engine._resolve_agent_name(target)
                if not matched:
                    await query.edit_message_text(f"❌ Agent '{target}' 不存在")
                    return
                targets = [matched]

            updated = []
            for name in targets:
                cfg = engine.registry.get(name)
                if cfg is not None:
                    cfg["reasoning_effort"] = effort
                    updated.append(name)
                    # Persist to disk
                    cfg_path = Path.home() / ".nanobot" / "agents" / name.lower() / "config.json"
                    if cfg_path.exists():
                        try:
                            disk_cfg = json.loads(cfg_path.read_text())
                            if effort:
                                disk_cfg["reasoning_effort"] = effort
                            else:
                                disk_cfg.pop("reasoning_effort", None)
                            cfg_path.write_text(json.dumps(disk_cfg, indent=2, ensure_ascii=False))
                        except Exception as e:
                            logger.warning("Failed to persist reasoning_effort for {}: {}", name, e)

            effort_display = effort or "off"
            names_str = ", ".join(updated)
            await query.answer(f"✅ {names_str} → {effort_display}", show_alert=False)

            # Refresh the status panel
            text, buttons = self._build_think_status_panel(engine)
            text = to_cli_style(text, title="🧠 Agent 思考模式")
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
            )

        elif data == "think_back":
            # Return to the main think status panel
            text, buttons = self._build_think_status_panel(engine)
            text = to_cli_style(text, title="🧠 Agent 思考模式")
            await query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
            )

