"""History settings callback handlers."""
from __future__ import annotations

from nanobot.channels.telegram.formatting import to_cli_style
from nanobot.channels.telegram.history_panel import (
    _SMART_SEARCH_SUMMARIZE_THRESHOLD,
    build_history_panel,
    build_stage3_panel,
    collect_live_metrics,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .param_docs import PARAM_DOCS


class HistoryCallbackMixin:
    """Mixin for hs_* inline keyboard callbacks."""

    async def _render_main_history_panel(self, query, *, expanded: bool = False) -> None:
        text, markup = build_history_panel(self._groupchat_engine, expanded=expanded)
        hist_text = to_cli_style(text, title="📚 上下文 & 历史")
        await query.edit_message_text(hist_text[:4096], reply_markup=markup, parse_mode="Markdown")

    async def _handle_history_callback(self, query, data: str) -> None:
        """Handle /history interactive settings callbacks."""
        from nanobot.groupchat.history import history_settings as hs

        if data == "hs_reload":
            hs.reload()
            await self._render_main_history_panel(query)
            return

        if data in ("hs_back", "hs_demo:0"):
            await self._render_main_history_panel(query, expanded=False)
            return

        if data == "hs_demo:1":
            await self._render_main_history_panel(query, expanded=True)
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
                "📝 Stage 1: process_tool_result 截断\n"
                "工具返回后按类型截断，再注入 tool_loop messages\n\n"
                f"━ {d_exec['label']} ━\n"
                f"  当前: {tr['exec_max_chars']:,} 字符 (head_tail)\n"
                f"  {d_exec['doc'].split(chr(10))[0]}\n\n"
                f"━ {d_web['label']} ━\n"
                f"  当前: {tr['web_fetch_max_chars']:,} 字符 (head_only)\n"
                f"  {d_web['doc'].split(chr(10))[0]}\n\n"
                f"━ {d_search['label']} ━\n"
                f"  当前: {tr['web_search_max_chars']:,} 字符 (head_only)\n"
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
                "🤖 Stage 2: 工具 AI 压缩 (配置项)\n"
                "⚠ 以下 summarize_* 尚未接入通用 tool_loop 管线\n"
                f"实际仅 SmartSearch 硬编码 {_SMART_SEARCH_SUMMARIZE_THRESHOLD:,} 字符\n\n"
                f"  配置开关 → {'✅ 开启' if enabled else '❌ 关闭'} (未接线)\n\n"
                f"━ {d_thresh['label']} ━\n"
                f"  当前: {tr['summarize_threshold']:,} 字符\n"
                f"  {d_thresh['doc'].split(chr(10))[0]}\n\n"
                f"━ {d_model['label']} ━\n"
                f"  当前: {tr['summarize_model']}\n"
                f"  (历史压缩 maybe_compress 使用此模型)\n\n"
                "broadcast/direct_result_max_chars: ⚠ 未接入注入路径 (仅 dedup 缓存)"
            )
            buttons = [
                [InlineKeyboardButton(f"{toggle_text} AI总结(未接线)", callback_data=f"hs_set:tool_results:summarize_enabled:{toggle_val}")],
                [InlineKeyboardButton(f"阈值: {tr['summarize_threshold']:,}", callback_data="hs_edit:tool_results:summarize_threshold")],
                [InlineKeyboardButton(f"最大输入: {tr.get('summarize_max_input_chars', 8000):,}", callback_data="hs_edit:tool_results:summarize_max_input_chars")],
                [InlineKeyboardButton(f"最大输出: {tr.get('summarize_max_output_chars', 4000):,}", callback_data="hs_edit:tool_results:summarize_max_output_chars")],
                [InlineKeyboardButton(f"广播模式: {tr.get('broadcast_result_max_chars', 20000):,}", callback_data="hs_edit:tool_results:broadcast_result_max_chars")],
                [InlineKeyboardButton(f"直接模式: {tr.get('direct_result_max_chars', 8000):,}", callback_data="hs_edit:tool_results:direct_result_max_chars")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_stage3":
            text, markup = build_stage3_panel(self._groupchat_engine)
            await query.edit_message_text(text, reply_markup=markup)

        elif data == "hs_stage4":
            settings = hs.get_all()
            cp = settings.get("context_pruning", {})
            metrics = collect_live_metrics(self._groupchat_engine)
            text = (
                "✂️ Stage 4: prune_messages (tool_loop iter≥2)\n"
                "当 estimate_tokens(messages)/context_window ≥ soft_ratio 时触发\n"
                "旧 tool result (>soft_max_chars) → 一行摘要\n\n"
                f"  软裁剪比例 → {cp.get('soft_ratio', 0.55)}"
                f" (当前历史 tok比 {metrics['tok_pct']}%)\n"
                f"  保护最近   → {cp.get('keep_recent', 4)} 个 assistant 轮\n"
                f"  软裁剪阈值 → {cp.get('soft_max_chars', 8000):,} 字符"
            )
            buttons = [
                [InlineKeyboardButton(f"软裁剪比例: {cp.get('soft_ratio', 0.55)}", callback_data="hs_edit:context_pruning:soft_ratio")],
                [InlineKeyboardButton(f"保护最近: {cp.get('keep_recent', 4)}", callback_data="hs_edit:context_pruning:keep_recent")],
                [InlineKeyboardButton(f"软裁剪阈值: {cp.get('soft_max_chars', 8000):,}", callback_data="hs_edit:context_pruning:soft_max_chars")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_stage5":
            settings = hs.get_all()
            tl = settings.get("tool_limits", {})
            text = (
                "🔧 Stage 5: 工具硬限制 (tool_limits)\n"
                "工具类内部的单次输出/超时硬上限\n"
                "原为类属性硬编码，现已集中到 settings\n\n"
                f"  read_file 输出上限 → {tl.get('read_file_max_chars', 64000):,} 字符\n"
                f"  read_file 默认行数 → {tl.get('read_file_default_lines', 300)} 行\n"
                f"  list_dir 默认条目  → {tl.get('list_dir_default_max', 200)} 条\n"
                f"  exec 最大超时      → {tl.get('exec_max_timeout', 600)} 秒\n"
                f"  exec 输出截断      → {tl.get('exec_max_output', 10000):,} 字符"
            )
            buttons = [
                [InlineKeyboardButton(f"read_file上限: {tl.get('read_file_max_chars', 64000):,}", callback_data="hs_edit:tool_limits:read_file_max_chars")],
                [InlineKeyboardButton(f"read_file行数: {tl.get('read_file_default_lines', 300)}", callback_data="hs_edit:tool_limits:read_file_default_lines")],
                [InlineKeyboardButton(f"list_dir条目: {tl.get('list_dir_default_max', 200)}", callback_data="hs_edit:tool_limits:list_dir_default_max")],
                [InlineKeyboardButton(f"exec超时: {tl.get('exec_max_timeout', 600)}s", callback_data="hs_edit:tool_limits:exec_max_timeout")],
                [InlineKeyboardButton(f"exec输出: {tl.get('exec_max_output', 10000):,}", callback_data="hs_edit:tool_limits:exec_max_output")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_stage6":
            settings = hs.get_all()
            pv = settings.get("tool_log_preview", {})
            text = (
                "📋 Stage 6: 工具日志预览 (tool_log_preview)\n"
                "<previous_tool_calls> 块中各工具结果的预览字符上限\n"
                "决定模型后续轮次能看到多少之前的工具调用结果\n\n"
                f"  web_search → {pv.get('web_search', 1500):,}  | "
                f"web_fetch → {pv.get('web_fetch', 1500):,}\n"
                f"  read_file  → {pv.get('read_file', 1500):,}  | "
                f"exec      → {pv.get('exec', 500):,}\n"
                f"  list_dir   → {pv.get('list_dir', 300):,}  | "
                f"_default  → {pv.get('_default', 500):,}\n"
                f"  write_file → {pv.get('write_file', 300):,}  | "
                f"edit_file  → {pv.get('edit_file', 300):,}\n"
                f"  chatroom   → {pv.get('chatroom_send', 200):,}  | "
                f"wait      → {pv.get('wait', 200):,}\n"
                f"  📌 总上限  → {pv.get('_total_cap', 4000):,} 字符"
            )
            buttons = [
                [InlineKeyboardButton(f"read_file预览: {pv.get('read_file', 1500):,}", callback_data="hs_edit:tool_log_preview:read_file")],
                [InlineKeyboardButton(f"write_file预览: {pv.get('write_file', 300):,}", callback_data="hs_edit:tool_log_preview:write_file")],
                [InlineKeyboardButton(f"edit_file预览: {pv.get('edit_file', 300):,}", callback_data="hs_edit:tool_log_preview:edit_file")],
                [InlineKeyboardButton(f"exec预览: {pv.get('exec', 500):,}", callback_data="hs_edit:tool_log_preview:exec")],
                [InlineKeyboardButton(f"web_search预览: {pv.get('web_search', 1500):,}", callback_data="hs_edit:tool_log_preview:web_search")],
                [InlineKeyboardButton(f"web_fetch预览: {pv.get('web_fetch', 1500):,}", callback_data="hs_edit:tool_log_preview:web_fetch")],
                [InlineKeyboardButton(f"list_dir预览: {pv.get('list_dir', 300):,}", callback_data="hs_edit:tool_log_preview:list_dir")],
                [InlineKeyboardButton(f"总上限: {pv.get('_total_cap', 4000):,}", callback_data="hs_edit:tool_log_preview:_total_cap")],
                [InlineKeyboardButton(f"默认预览: {pv.get('_default', 500):,}", callback_data="hs_edit:tool_log_preview:_default")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("hs_set:"):
            parts = data.split(":", 3)
            if len(parts) == 4:
                section, key, raw_val = parts[1], parts[2], parts[3]
                if raw_val in ("true", "false"):
                    value = raw_val == "true"
                elif raw_val.isdigit():
                    value = int(raw_val)
                else:
                    value = raw_val
                result = hs.update_field(section, key, value)
                await query.answer(result, show_alert=True)
                if section == "tool_results" and ("summarize" in key or "result_max_chars" in key):
                    await self._handle_history_callback(query, "hs_stage2")
                elif section == "context_pruning":
                    await self._handle_history_callback(query, "hs_stage4")
                elif section == "history":
                    await self._handle_history_callback(query, "hs_stage3")
                elif section == "tool_limits":
                    await self._handle_history_callback(query, "hs_stage5")
                elif section == "tool_log_preview":
                    await self._handle_history_callback(query, "hs_stage6")
                else:
                    await self._handle_history_callback(query, "hs_back")

        elif data.startswith("hs_edit:"):
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