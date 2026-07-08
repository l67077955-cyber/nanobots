"""Telegram prompt order/edit callbacks."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from loguru import logger

from nanobot.groupchat import display as _d
from nanobot.groupchat.history.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
    COMPONENT_PHASES as _COMPONENT_PHASES,
)
from ..formatting import TELEGRAM_MAX_MESSAGE_LEN, to_cli_style


class PromptsCallbackMixin:
    async def _dispatch_prompts(self, query, data: str, chat_id: str) -> bool:
        if data == "prmanage":
            # Toggle manage mode for prompt order view
            self._edit_state.pop(chat_id, None)  # leave any pending template edit
            self._prompt_manage_mode = not getattr(self, '_prompt_manage_mode', False)
            await self._prompt_show_components(query, manage_mode=self._prompt_manage_mode)

            return True

        if data in ("pr:refresh", "pr:"):
            # Refresh global prompt order view (exits manage mode)
            self._edit_state.pop(chat_id, None)  # leave any pending template edit
            self._prompt_manage_mode = False
            await self._prompt_show_components(query)

            return True

        if data.startswith("pre:"):
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
                        [InlineKeyboardButton("❌ 取消", callback_data="prcan")],
                        [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
                    ]),
                )

            return True

        if data == "prcan":
            # Cancel edit
            self._edit_state.pop(chat_id, None)
            await self._prompt_show_components(query, manage_mode=getattr(self, '_prompt_manage_mode', False))

            return True

        if data.startswith("pru:") or data.startswith("prd:"):
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

            return True

        if data.startswith("pviz:"):
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

            return True

        if data.startswith("prdel:"):
            # Delete component: prdel:<idx>
            idx = int(data[6:])
            engine = self._groupchat_engine
            result = engine.prompt_builder.remove_prompt_component(idx)
            await query.answer(result, show_alert=True)
            await self._prompt_show_components(query, manage_mode=getattr(self, '_prompt_manage_mode', False))

            return True

        if data == "pradd":
            # Show available components to add back
            self._edit_state.pop(chat_id, None)  # leave any pending template edit
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
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
            await query.edit_message_text(
                "➕ 选择要添加的组件:\n\n💡 点击 \"✏️ 自定义组件名\" 创建全新组件",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

            return True

        if data.startswith("pradd:"):
            # Add component back: pradd:<key>
            key = data[6:]
            engine = self._groupchat_engine
            order = engine.prompt_builder.get_agent_prompt_order()
            if key not in order:
                order.append(key)
                engine.prompt_builder.set_default_prompt_order(order)
            await self._prompt_show_components(query)

            return True

        if data == "pradd_custom":
            # Enter edit state for user to type a custom component name
            chat_id = str(query.message.chat_id)
            self._edit_state[chat_id] = {"field": "pradd_custom_name"}
            await query.edit_message_text(
                "✏️ 创建自定义提示词组件\n\n"
                "请输入组件名称（如: 角色背景、安全规则、写作风格 等）:\n\n"
                "💡 名称会显示在组件列表中，创建后可选 Phase 类型",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ 取消", callback_data="prcan")],
                    [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
                ]),
            )

            return True

        if data == "prrules":
            # Show prompt assembly rules explanation
            self._edit_state.pop(chat_id, None)  # leave any pending template edit
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
                    [InlineKeyboardButton("⬅️ 返回组件列表", callback_data="pr:refresh")],
                    [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
                ]),
            )

            return True

        if data.startswith("prv:"):
            # Preview full template: prv:<page>
            self._edit_state.pop(chat_id, None)  # leave any pending template edit
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
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
            await query.edit_message_text(
                page_text[:4096],
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        # ── Provider/Model management callbacks ──
            return True

        return False
