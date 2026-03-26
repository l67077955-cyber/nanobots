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
from nanobot.groupchat.prompt_builder import (
    PromptBuilder, COMPONENT_LABELS as _COMPONENT_LABELS,
    GLOBAL_EDITABLE as _GLOBAL_EDITABLE, AGENT_EDITABLE as _AGENT_EDITABLE,
)
from .formatting import TELEGRAM_MAX_MESSAGE_LEN


class CallbacksMixin:
    """Mixin providing inline keyboard callback handling."""

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle InlineKeyboard button presses."""
        query = update.callback_query
        if not query or not query.data:
            return
        logger.debug("Callback received: data={} from={}", query.data, query.from_user.id if query.from_user else "?")
        await query.answer()

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
                from nanobot.groupchat.engine import GroupChatEngine
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
            if not engine or (not engine._history and not engine._request_log):
                await query.edit_message_text("📭 无日志")
                return
            rlog = engine._request_log
            history = engine._history
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

        elif data.startswith("tf:"):
            # tf:AgentName:tool_name — toggle individual tool
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            name, tool = parts[1], parts[2]
            from nanobot.groupchat.engine import GroupChatEngine
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
                if cfg_path.exists():
                    try:
                        cfg = json.loads(cfg_path.read_text())
                        cfg["tools"] = tools_cfg
                        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
                    except Exception:
                        pass

            # Refresh buttons by re-triggering tools menu
            labels = {
                "web_search": "🔍 网页搜索", "web_fetch": "🌐 网页抓取",
                "exec": "⚡ 执行命令", "read_file": "📄 读文件",
                "write_file": "✍️ 写文件", "edit_file": "✂️ 编辑文件",
                "list_dir": "📁 列目录",
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

        elif data.startswith("rlog:"):
            # Persistent log detail view
            idx = int(data[5:])
            logs = self._load_request_logs()
            if idx >= len(logs):
                await query.edit_message_text("⚠️ 记录不存在")
                return
            r = logs[idx]

            def _trunc(s: str, limit: int) -> str:
                if len(s) <= limit:
                    return s
                return s[:limit] + f"…(还有{len(s)-limit}字)"

            model = r.get("model", "?")
            agent = r.get("agent") or "?"
            session = r.get("session") or "?"
            topic = r.get("topic") or ""
            mode = r.get("mode") or "?"
            lines = [
                f"--- 请求 #{idx+1} ---",
                f"agent={agent} mode={mode}",
                f"session={session}",
            ]
            if topic:
                lines.append(f"topic={topic}")
            lines += [
                f"model={model}",
                f"api_base={r.get('api_base', '(default)')}",
                f"ts={r.get('ts', '?')} latency={r.get('latency', 0)}s",
                f"max_tokens={r.get('max_tokens', '?')} stream={'是' if r.get('stream') else '否'}",
                f"status={'✅ 成功' if r.get('status') == 'ok' else '❌ 失败'}",
            ]

            # Params
            params = r.get("params", {})
            if params:
                ps = " ".join(f"{k}={v}" for k, v in params.items() if v is not None)
                if ps:
                    lines.append(f"params: {ps}")

            # Messages summary
            msgs = r.get("messages", [])
            if msgs:
                role_counts: dict[str, int] = {}
                for m in msgs:
                    rl = m.get("role", "?")
                    role_counts[rl] = role_counts.get(rl, 0) + 1
                rc_str = " ".join(f"{rl}={c}" for rl, c in role_counts.items())
                lines.append(f"msgs: {len(msgs)} ({rc_str}) total={r.get('total_chars', 0)}字")

            # Tools
            tc_count = r.get("tools_count", 0)
            if tc_count:
                lines.append(f"tools_count: {tc_count}")

            # Usage
            usage = r.get("usage", {})
            if usage:
                lines.append(
                    f"tokens: prompt={usage.get('prompt', 0)} "
                    f"compl={usage.get('completion', 0)} "
                    f"total={usage.get('total', 0)}"
                )

            # Cost & cache
            cost = r.get("cost")
            cache_t = r.get("cache_tokens")
            if cost:
                lines.append(f"💰 cost: ${cost:.6f}")
            if cache_t:
                lines.append(f"🔵 cache: {cache_t} tokens")

            # Provider metadata (OpenRouter etc.)
            pmeta_list = r.get("provider_meta", [])
            if pmeta_list:
                pm = pmeta_list[0] if isinstance(pmeta_list, list) else pmeta_list
                if isinstance(pm, dict) and pm:
                    lines.append("")
                    lines.append("📡 Provider:")
                    if pm.get("model_id"):
                        lines.append(f"  model: {pm['model_id']}")
                    if pm.get("provider"):
                        lines.append(f"  provider: {pm['provider']}")
                    if pm.get("generation_id"):
                        lines.append(f"  gen_id: {pm['generation_id']}")
                    if pm.get("latency_ms"):
                        lines.append(f"  latency: {pm['latency_ms']}ms")
                    if pm.get("tps"):
                        lines.append(f"  throughput: {pm['tps']} tps")
                    if pm.get("reasoning_tokens"):
                        lines.append(f"  reasoning: {pm['reasoning_tokens']} tokens")
                    if pm.get("final_cost") is not None:
                        lines.append(f"  final_cost: ${pm['final_cost']:.6f}")
                    if pm.get("cache_tokens"):
                        lines.append(f"  cached: {pm['cache_tokens']} tokens")

            # Response
            reply_len = r.get("reply_len", 0)
            finish = r.get("finish_reason", "")
            lines.append(f"reply: {reply_len}字 finish={finish}")

            if r.get("has_tool_calls"):
                tc_list = r.get("reply_tool_calls", [])
                for tc in tc_list[:5]:
                    lines.append(f"  🔧 {tc.get('name', '?')} args={tc.get('args_len', tc.get('args_preview', '?'))}")

            # Reply preview
            preview = r.get("reply_preview", "")
            if preview:
                lines.append(f"\n[回复预览]\n{_trunc(preview, 300)}")

            # Error
            if r.get("error"):
                lines.append(f"\n[错误] {r.get('error_type', '?')}")
                lines.append(_trunc(r["error"], 300))
                sc = r.get("status_code")
                if sc:
                    lines.append(f"http_status={sc}")

            text = "\n".join(lines)
            page = idx // 8
            buttons = []
            if msgs:
                buttons.append([InlineKeyboardButton("📝 完整请求内容", callback_data=f"rlogp:{idx}:0")])
            buttons.append([InlineKeyboardButton("⬅️ 返回列表", callback_data=f"rlog_pg:{page}")])
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

        elif data.startswith("mode:"):
            mode = data[5:]
            result = self._groupchat_engine.set_mode(mode)
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
                    f"✏️ 修改 {key}\n"
                    f"当前值: {params[key]}\n\n"
                    f"请输入新值 (数字):"
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
                except Exception:
                    pass
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
                "➕ 选择要添加的参数:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        elif data.startswith("hp_new:"):
            key = data[7:]
            self._edit_state[chat_id] = {"field": "hp_value", "hp_key": key, "hp_is_new": True}
            await query.edit_message_text(f"➕ 添加 {key}\n\n请输入值 (数字):")

        elif data == "hp_custom":
            self._edit_state[chat_id] = {"field": "hp_add_custom"}
            await query.edit_message_text("✏️ 请输入参数名:")

        elif data == "hp_back":
            provider = getattr(self._groupchat_engine, 'provider', None) if self._groupchat_engine else None
            params = getattr(provider, 'sampling_params', None) if provider else {}
            await query.edit_message_text("⚙️ 返回...")
            await self._send_hyperparams_keyboard(chat_id, params)

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
        elif data.startswith("pr:"):
            # Refresh global prompt order view
            await self._prompt_show_components(query)

        elif data.startswith("pre:"):
            # Edit global template: pre:__global__:component_key
            parts = data[4:].split(":", 1)
            if len(parts) == 2:
                _, key = parts
                engine = self._groupchat_engine
                overrides = PromptBuilder._load_prompt_overrides("__global__")
                content = overrides.get(key) or PromptBuilder.get_component_template(key)
                label = _COMPONENT_LABELS.get(key, key)
                self._edit_state[chat_id] = {"field": "prompt_edit", "agent": "__global__", "key": key}
                preview = (content[:3500] + "…") if len(content) > 3500 else (content or "(空)")
                await query.edit_message_text(
                    f"✏️ 编辑全局模板 - {label}\n\n"
                    f"当前内容 ({len(content or '')}字):\n"
                    f"{preview}\n\n"
                    f"💡 模板变量: {{{{agent}}}} {{{{members}}}} {{{{datetime}}}} {{{{round}}}} {{{{tools}}}} {{{{others}}}}\n"
                    f"请回复新内容 (完整替换):",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ 取消", callback_data="prcan")]
                    ]),
                )

        elif data == "prcan":
            # Cancel edit
            self._edit_state.pop(chat_id, None)
            await self._prompt_show_components(query)

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
            await self._prompt_show_components(query)

        elif data.startswith("prdel:"):
            # Delete component: prdel:<idx>
            idx = int(data[6:])
            engine = self._groupchat_engine
            result = engine.prompt_builder.remove_prompt_component(idx)
            await query.answer(result, show_alert=True)
            await self._prompt_show_components(query)

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
                "💡 名称会显示在组件列表中，创建后可编辑内容",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ 取消", callback_data="prcan")]
                ]),
            )

        elif data.startswith("prv:"):
            # Preview full template: prv:<page>
            page = int(data[4:])
            engine = self._groupchat_engine
            order = engine.prompt_builder.get_agent_prompt_order()
            overrides = PromptBuilder._load_prompt_overrides("__global__")
            labels = _COMPONENT_LABELS

            lines: list[str] = []
            # Dynamic components show markers instead of placeholder text
            dynamic_markers = {
                "persona": "(→ 运行时加载每个 agent 的 SOUL.md)",
                "history": "(→ 运行时自动插入聊天记录)",
            }
            for i, key in enumerate(order):
                label = labels.get(key, key)
                if key in dynamic_markers:
                    lines.append(f"═══ [{i+1}] {label} ═══")
                    lines.append(dynamic_markers[key])
                    lines.append("")
                    continue
                tpl = overrides.get(key) or PromptBuilder.get_component_template(key)
                if not tpl:
                    continue
                lines.append(f"═══ [{i+1}] {label} ({len(tpl)}字) ═══")
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

            total_pages = max(1, (len(filtered) + per_page - 1) // per_page)
            page = min(page, total_pages - 1)
            start = page * per_page
            page_items = filtered[start:start + per_page]

            lines = [f"📋 {prov} / {prefix} ({len(filtered)}) [第{page+1}/{total_pages}页]:\n"]
            buttons = []
            for mid in page_items:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪️ {mid}")
                    cb = f"ep_addm:{prov}:{mid}"
                    if len(cb.encode()) <= 64:
                        buttons.append([InlineKeyboardButton(f"+ {mid}", callback_data=cb)])
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
            lines = [f"📋 {prov} / {prefix} ({len(filtered)}):\n"]
            buttons = []
            for mid in filtered[:30]:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪️ {mid}")
                    cb = f"ep_addm:{prov}:{mid}"
                    if len(cb.encode()) <= 64:
                        buttons.append([InlineKeyboardButton(f"+ {mid}", callback_data=cb)])
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

    async def _handle_history_callback(self, query, data: str) -> None:
        """Handle /history interactive settings callbacks."""
        from nanobot.groupchat import history_settings as hs
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if data == "hs_reload":
            hs.reload()
            await query.edit_message_text("🔄 配置已重载")
            return

        if data == "hs_global":
            settings = hs.get_all()
            text = (
                "🌐 全局设置\n\n"
                "影响记忆合并和工具结果保存的核心参数：\n\n"
                f"  上下文窗口 → {settings['context_window_tokens']:,} tokens\n"
                f"  工具结果截断 → {settings['tool_result_max_chars']:,} 字符\n\n"
                "上下文窗口: 超过时自动合并旧消息为摘要\n"
                "工具结果截断: 保存到会话时的最大字符数"
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
            text = (
                "📝 Stage 1: 工具输出截断\n\n"
                "工具返回长文本时，先在源头截断：\n\n"
                f"  exec       → {tr['exec_max_chars']:,} 字符\n"
                f"  web_fetch  → {tr['web_fetch_max_chars']:,} 字符\n"
                f"  web_search → {tr['web_search_max_chars']:,} 字符\n\n"
                "截断方式: 保留首尾各一半，中间标记 (N chars truncated)"
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
            text = (
                "🤖 Stage 2: AI 总结压缩\n\n"
                "工具结果超过阈值时，用小模型提取关键信息：\n\n"
                f"  状态   → {'✅ 开启' if enabled else '❌ 关闭'}\n"
                f"  阈值   → {tr['summarize_threshold']:,} 字符\n"
                f"  模型   → {tr['summarize_model']}\n\n"
                "流程: raw → LLM提取关键信息 → 压缩后注入上下文\n"
                "失败兜底: head+tail 截断"
            )
            buttons = [
                [InlineKeyboardButton(f"{toggle_text} AI总结", callback_data=f"hs_set:tool_results:summarize_enabled:{toggle_val}")],
                [InlineKeyboardButton(f"阈值: {tr['summarize_threshold']:,}", callback_data="hs_edit:tool_results:summarize_threshold")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_stage3":
            settings = hs.get_all()
            hist = settings["history"]
            engine = self._groupchat_engine
            current_msgs = len(engine._history) if engine else 0
            current_chars = sum(len(m.get("content", "")) for m in (engine._history if engine else []))
            text = (
                "📚 Stage 3: 历史存储\n\n"
                "对话历史超过限制时，丢弃最早消息：\n\n"
                f"  最大消息数 → {hist['max_messages']} 条\n"
                f"  最大上下文 → {hist['max_context_chars']:,} 字符\n\n"
                f"  当前 → {current_msgs} 条 / {current_chars:,} 字符"
            )
            buttons = [
                [InlineKeyboardButton(f"消息数: {hist['max_messages']}", callback_data="hs_edit:history:max_messages")],
                [InlineKeyboardButton(f"上下文: {hist['max_context_chars']:,}", callback_data="hs_edit:history:max_context_chars")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data == "hs_back":
            # Rebuild main /history view
            settings = hs.get_all()
            tr = settings["tool_results"]
            hist = settings["history"]
            engine = self._groupchat_engine
            current_msgs = len(engine._history) if engine else 0
            current_chars = sum(len(m.get("content", "")) for m in (engine._history if engine else []))
            summarize_status = "✅ 开启" if tr["summarize_enabled"] else "❌ 关闭"
            text = (
                "📊 历史管理流程\n\n"
                "━━ 全局设置 ━━\n"
                f"  上下文窗口 → {settings['context_window_tokens']:,} tokens\n"
                f"  工具结果截断 → {settings['tool_result_max_chars']:,} 字符\n\n"
                "━━ Stage 1: 工具输出截断 ━━\n"
                f"  exec       → 最大 {tr['exec_max_chars']:,} 字符\n"
                f"  web_fetch  → 最大 {tr['web_fetch_max_chars']:,} 字符\n"
                f"  web_search → 最大 {tr['web_search_max_chars']:,} 字符\n\n"
                "━━ Stage 2: AI 总结压缩 ━━\n"
                f"  触发阈值 → {tr['summarize_threshold']:,} 字符\n"
                f"  总结模型 → {tr['summarize_model']}\n"
                f"  状态     → {summarize_status}\n\n"
                "━━ Stage 3: 历史存储 ━━\n"
                f"  最大消息数 → {hist['max_messages']} 条\n"
                f"  最大上下文 → {hist['max_context_chars']:,} 字符\n"
                f"  当前消息数 → {current_msgs} 条\n"
                f"  当前上下文 → {current_chars:,} 字符\n"
            )
            buttons = [
                [
                    InlineKeyboardButton("🌐 全局设置", callback_data="hs_global"),
                    InlineKeyboardButton("📝 工具截断", callback_data="hs_stage1"),
                ],
                [
                    InlineKeyboardButton("🤖 AI总结", callback_data="hs_stage2"),
                    InlineKeyboardButton("📚 历史限制", callback_data="hs_stage3"),
                ],
                [
                    InlineKeyboardButton("🔄 重载配置", callback_data="hs_reload"),
                ],
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

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
                # Refresh stage view
                if section == "tool_results" and "summarize" in key:
                    await self._handle_history_callback(query, "hs_stage2")
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
                label = key
                if section != "__top__":
                    label = f"{section}.{key}"
                await query.edit_message_text(
                    f"✏️ 修改 {label}\n\n"
                    f"当前值: {current:,}\n\n"
                    f"请输入新值 (数字):"
                )

    async def _handle_edit_input(self, chat_id: str, content: str) -> None:
        """Process interactive edit state input."""
        state = self._edit_state[chat_id]
        logger.debug("Edit input: chat_id={} field={} content={}...", chat_id, state.get("field", "?"), content[:50])

        # History setting value input
        if state.get("action") == "history_setting":
            del self._edit_state[chat_id]
            section = state["section"]
            key = state["key"]
            try:
                value = int(content.strip())
            except ValueError:
                await self._gc_send(chat_id, f"❌ 请输入数字，收到: {content.strip()}")
                return
            from nanobot.groupchat import history_settings as hs
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
            lines = [f"🔍 搜索 \"{content.strip()}\" ({len(filtered)} 结果):\n"]
            buttons = []
            for mid in filtered[:25]:
                if mid in existing:
                    lines.append(f"  ✅ {mid}")
                else:
                    lines.append(f"  ⚪️ {mid}")
                    cb = f"ep_addm:{prov}:{mid}"
                    if len(cb.encode()) <= 64:
                        buttons.append([InlineKeyboardButton(f"+ {mid}", callback_data=cb)])
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
            try:
                value = float(content.strip())
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
                except Exception:
                    pass
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
                engine.registry[name] = {"model": model, "prompt": prompt}
                # Save to disk
                from pathlib import Path as _P
                soul_dir = _P.home() / ".nanobot" / "agents" / name.lower() / "workspace"
                soul_dir.mkdir(parents=True, exist_ok=True)
                (soul_dir / "SOUL.md").write_text(prompt)
                config_path = soul_dir.parent / "config.json"
                import json
                config_path.write_text(json.dumps({"model": model}, indent=2))
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
                # Rename directory
                from pathlib import Path as _P
                agents_dir = _P.home() / ".nanobot" / "agents"
                old = agents_dir / agent_name.lower()
                new = agents_dir / new_name.lower()
                if old.exists() and not new.exists():
                    old.rename(new)
                engine._save_active()
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
            import json
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

