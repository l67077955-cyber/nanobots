"""History settings callback handlers."""
from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .param_docs import PARAM_DOCS


class HistoryCallbackMixin:
    """Mixin for hs_* inline keyboard callbacks."""

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

