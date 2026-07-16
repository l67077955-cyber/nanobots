"""Telegram agent/group/hyperparam callbacks."""
from __future__ import annotations

import json
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from loguru import logger


class AgentCallbackMixin:
    async def _dispatch_agents(self, query, data: str, chat_id: str) -> bool:
        if data == "al":
            # Show agent list as inline keyboard
            if not self._groupchat_engine:
                await query.edit_message_text("⚠️ 无 agent")
                return True
            registry = self._groupchat_engine.registry
            active = self._groupchat_engine.active_agents
            buttons = []
            for name in registry:
                status = "🟢" if name in active else "⚪"
                buttons.append([InlineKeyboardButton(
                    f"{status} {name}",
                    callback_data=f"edit:{name}"
                )])
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
            await query.edit_message_text(
                "📋 Agent 列表 — 点击名称编辑:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            return True

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
            self._sync_agent_settings_from_disk(name)
            agent = self._groupchat_engine.registry.get(name)
            if not agent:
                await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                return True
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
                return True
            await query.edit_message_text(
                f"🗑️ 删除 Agent: {name}\n\n"
                f"模型: {agent.get('model', '?')}\n\n"
                "⚠️ 此操作将永久删除该 agent 的配置文件，无法恢复！\n"
                "确认删除吗？",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ 确认删除", callback_data=f"dac:{name}:yes")],
                    [InlineKeyboardButton("❌ 取消", callback_data=f"edit:{name}")],
                    [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
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
                return True
            engine = self._groupchat_engine
            if not engine or name not in engine.registry:
                await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                return True
        
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
                # Redirect to agent list instead of dead-end text
                return await self._dispatch_agents(query, "al", chat_id)
            if field == "tools":
                # Show per-tool toggle buttons
                from nanobot.groupchat.runtime.engine import GroupChatEngine
                agent = self._groupchat_engine.registry.get(name, {})
                tools_cfg = agent.get("tools")
                # Migrate legacy tools_enabled → granular dict only when there
                # is no dict yet. If a dict exists but is missing some tool
                # keys, backfill the missing keys with the legacy default
                # WITHOUT dropping the user's existing per-tool toggles.
                if not isinstance(tools_cfg, dict):
                    all_on = agent.get("tools_enabled", False)
                    tools_cfg = {t: all_on for t in GroupChatEngine.TOOL_NAMES}
                    agent["tools"] = tools_cfg
                else:
                    missing = [
                        t for t in GroupChatEngine.TOOL_NAMES if t not in tools_cfg
                    ]
                    if missing:
                        all_on = agent.get("tools_enabled", False)
                        for t in missing:
                            tools_cfg[t] = all_on

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
                buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
                await query.edit_message_text(
                    f"🔧 {name} 工具权限设置:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return True
            elif field == "rank":
                from nanobot.groupchat.context.ranks import RANK_DISPLAY, RANK_ORDER, resolve_rank

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
                buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
                await query.edit_message_text(
                    f"🎖️ {name} 等级设置 (当前: {current_label})\n\n"
                    f"更改 rank 会立即更新中断权限；对话池/搜索额度在本轮内不变，新轮次生效。",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return True
            elif field == "hyperparams":
                # Per-agent hyperparams (same UX as /hyperparams but per-agent)
                agent = self._sync_agent_settings_from_disk(name)
                agent_hp = agent.get("hyperparams") or {}
                await self._send_agent_hyperparams_keyboard(chat_id, name, agent_hp, query=query)
                return True
            elif field == "reasoning_effort":
                # Show effort level selection — friendly "思考深度"
                agent = self._sync_agent_settings_from_disk(name)
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
                return True
            elif field == "presets":
                # Simple high-level presets that hide complexity
                buttons = [
                    [InlineKeyboardButton("⚖️ 平衡（推荐）", callback_data=f"preset:{name}:balanced")],
                    [InlineKeyboardButton("✨ 更有创意", callback_data=f"preset:{name}:creative")],
                    [InlineKeyboardButton("🔬 更严谨分析", callback_data=f"preset:{name}:precise")],
                    [InlineKeyboardButton("🧠 深度思考", callback_data=f"preset:{name}:deep")],
                    [InlineKeyboardButton("↩️ 恢复默认", callback_data=f"preset:{name}:reset")],
                    [InlineKeyboardButton("⬅️ 返回", callback_data=f"edit:{name}")],
                    [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
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
                return True
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
                    buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
                    await query.edit_message_text("🤖 选择提供商:", reply_markup=InlineKeyboardMarkup(buttons))
                else:
                    await query.edit_message_text("请输入新模型名 (如 anthropic/claude-sonnet-4-5):")
            else:
                prompts = {"name": "新名字"}
                await query.edit_message_text(f"请输入{prompts.get(field, field)}:")

        if data.startswith("ef_re:"):
            # ef_re:AgentName:level — set reasoning effort
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            name, lvl = parts[1], parts[2]
            engine = self._groupchat_engine
            if not engine or name not in engine.registry:
                await query.edit_message_text(f"❌ Agent '{name}' 不存在")
                return True

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
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
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

            async def _refresh_edit_menu(status: str) -> None:
                await query.edit_message_text(
                    f"{status}\n\n{self._edit_menu_text(name)}",
                    reply_markup=self._edit_menu_buttons(name),
                )

            def _apply_and_persist(changes: dict, msg: str):
                # Apply to runtime
                hp_changes = {k: v for k, v in changes.items() if k != "reasoning_effort"}
                if hp_changes:
                    hp = agent.setdefault("hyperparams", {})
                    hp.update(hp_changes)
                if "reasoning_effort" in changes:
                    agent["reasoning_effort"] = changes["reasoning_effort"]
                # Persist
                try:
                    if cfg_path.exists():
                        c = json.loads(cfg_path.read_text())
                        if hp_changes:
                            c.setdefault("hyperparams", {})
                            c["hyperparams"].update(hp_changes)
                        if "reasoning_effort" in changes:
                            c["reasoning_effort"] = changes["reasoning_effort"]
                        cfg_path.write_text(json.dumps(c, indent=2, ensure_ascii=False))
                except Exception as e:
                    logger.warning("Preset persist partial fail: {}", e)
                return msg

            if preset == "balanced":
                # Clear heavy overrides, set medium
                for k in list(agent.get("hyperparams", {}).keys()):
                    if k in ("temperature", "top_p", "reasoning_effort"):
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
                await _refresh_edit_menu(f"✅ {name} 已设为「平衡」预设（中等思考深度，默认采样）。")
                return True

            elif preset == "creative":
                _apply_and_persist(
                    {"temperature": 0.9, "top_p": 0.95, "reasoning_effort": "medium"},
                    "更有创意"
                )
                await _refresh_edit_menu(f"✅ {name} 已应用「更有创意」预设：更高随机性 + 中等思考深度。")
                return True

            elif preset == "precise":
                _apply_and_persist(
                    {"temperature": 0.2, "top_p": 0.9, "reasoning_effort": "medium"},
                    "更严谨"
                )
                await _refresh_edit_menu(f"✅ {name} 已应用「更严谨分析」预设：低温度严谨采样 + 中等思考。")
                return True

            elif preset == "deep":
                _apply_and_persist(
                    {"temperature": 0.5, "top_p": 0.9, "reasoning_effort": "high"},
                    "深度思考"
                )
                await _refresh_edit_menu(
                    f"✅ {name} 已应用「深度思考」预设：高思考强度（适合支持推理的模型）+ 适中采样。"
                )
                return True

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
                await _refresh_edit_menu(f"✅ {name} 已恢复默认（清除超参数与思考深度覆盖）。")
                return True

        elif data.startswith("tf:"):
            # tf:AgentName:tool_name — toggle individual tool
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            name, tool = parts[1], parts[2]
            from nanobot.groupchat.runtime.engine import GroupChatEngine
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
                # Default agent (Nanobot): its config lives under
                # config.json → agents.defaults (same place its model is
                # persisted). The old nanobot_tools.json file was never read
                # back, so toggles were lost on restart.
                main_cfg_path = Path.home() / ".nanobot" / "config.json"
                try:
                    cfg = (
                        json.loads(main_cfg_path.read_text())
                        if main_cfg_path.exists() else {}
                    )
                    cfg.setdefault("agents", {}).setdefault("defaults", {})["tools"] = tools_cfg
                    main_cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                except Exception as e:
                    logger.warning("Failed to persist default-agent tools: {}", e)
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
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
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

            from nanobot.groupchat.context.ranks import RANK_DISPLAY, RANK_ORDER

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

        if data.startswith("sl:"):
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
            self._sync_global_hyperparams_from_disk()
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
            self._sync_global_hyperparams_from_disk()
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
            params = self._sync_global_hyperparams_from_disk()
            common = ["temperature", "top_p", "top_k", "min_p", "top_a",
                      "frequency_penalty", "presence_penalty", "repetition_penalty"]
            available = [p for p in common if p not in params]
            buttons = []
            for p in available:
                buttons.append([InlineKeyboardButton(f"➕ {p}", callback_data=f"hp_new:{p}")])
            buttons.append([InlineKeyboardButton("✏️ 自定义参数名", callback_data="hp_custom")])
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="hp_back")])
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
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
            params = self._sync_global_hyperparams_from_disk()
            await query.edit_message_text("⚙️ 返回...")
            await self._send_hyperparams_keyboard(chat_id, params)

        # ── Agent Hyperparams (ahp:) ──────────────────────────
        elif data.startswith("ahp:"):
            # ahp:AgentName:key
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            a_name, key = parts[1], parts[2]
            agent = self._sync_agent_settings_from_disk(a_name) if self._groupchat_engine else {}
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
                agent = self._sync_agent_settings_from_disk(a_name)
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
                    await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent_hp, query=query)

        elif data.startswith("ahp_sync:"):
            # ahp_sync:AgentName
            a_name = data[9:]
            if self._groupchat_engine and a_name in self._groupchat_engine.registry:
                global_hp = {}
                global_hp = self._sync_global_hyperparams_from_disk()
                if not global_hp:
                    provider = getattr(self._groupchat_engine, 'provider', None)
                    if provider and hasattr(provider, 'sampling_params'):
                        global_hp = dict(provider.sampling_params)

                if global_hp:
                    global_hp = {k: v for k, v in global_hp.items() if k != "reasoning_effort"}
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
                    await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent_hp, query=query)
                else:
                    await query.answer("⚠️ 全局超参数为空", show_alert=True)

        elif data.startswith("ahp_add:"):
            # ahp_add:AgentName
            a_name = data[8:]
            agent = self._sync_agent_settings_from_disk(a_name) if self._groupchat_engine else {}
            agent_hp = agent.get("hyperparams") or {}
            common = ["temperature", "top_p", "top_k", "min_p", "top_a",
                      "frequency_penalty", "presence_penalty", "repetition_penalty"]
            available = [p for p in common if p not in agent_hp]
            buttons = []
            for p in available:
                buttons.append([InlineKeyboardButton(f"➕ {p}", callback_data=f"ahp_new:{a_name}:{p}")])
            buttons.append([InlineKeyboardButton("✏️ 自定义参数名", callback_data=f"ahp_custom:{a_name}")])
            buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"ahp_back:{a_name}")])
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
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
            agent = self._sync_agent_settings_from_disk(a_name) if self._groupchat_engine else {}
            agent_hp = agent.get("hyperparams") or {}
            await query.edit_message_text("⚙️ 返回...")
            await self._send_agent_hyperparams_keyboard(chat_id, a_name, agent_hp, query=query)

        elif data.startswith("gc:"):
            key = data[3:]
            settings = self._load_gc_settings()
            label = self.GC_SETTINGS_LABELS.get(key, key)
            val = settings.get(key, self.GC_SETTINGS_DEFAULTS.get(key, "?"))
            self._edit_state[chat_id] = {"field": "gc_value", "gc_key": key}
            await query.edit_message_text(
                f"✏️ 修改 {label}\n"
                f"当前值: {val}\n\n"
                f"请输入新值 (数字):"
            )

        elif data.startswith("ord:"):
            val = data[4:]
            if val == "done":
                agents = self._groupchat_engine.active_agents
                # Persist the active order only. Do NOT silently overwrite the
                # saved group that happened to be loaded — reordering a loaded
                # group is a transient active change; the user can /savegroup
                # explicitly if they want to update the saved roster.
                self._groupchat_engine.save_active()
                order_str = " → ".join(agents)
                await query.edit_message_text(
                    f"📢 发言顺序（当前会话）:\n{order_str}\n\n"
                    "ℹ️ 仅当前会话生效。如需更新已保存的分组，请用 /savegroup。"
                )
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

        return False
