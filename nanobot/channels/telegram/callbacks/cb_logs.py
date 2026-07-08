"""Telegram session log callbacks."""
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


class LogsCallbackMixin:
    async def _dispatch_logs(self, query, data: str, chat_id: str) -> bool:
        if data.startswith("log:"):
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

            return True

        if data.startswith("log_pg:"):
            page = int(data[7:])
            logs = self._groupchat_engine.request_log
            text, markup = self._build_log_page_v2(logs, page)
            await query.edit_message_text(text, reply_markup=markup)

            return True

        if data.startswith("rlog_pg:"):
            page = int(data[8:])
            logs = self._load_request_logs()
            text, markup = self._build_log_page_v2(logs, page)
            await query.edit_message_text(text, reply_markup=markup)

            return True

        if data.startswith("rlogs_pg:"):
            # Search-filtered pagination
            page = int(data[9:])
            logs = self._load_request_logs()
            kw = getattr(self, "_log_search", {}).get(chat_id, "")
            if kw:
                logs = self._filter_logs(logs, kw)
            text, markup = self._build_log_page_v2(logs, page, keyword=kw)
            await query.edit_message_text(text, reply_markup=markup)

            return True

        if data.startswith("rlogp:"):
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
            return True

        if data.startswith("rlog_dl:"):
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

            return True

        if data.startswith("rlog:"):
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
                [InlineKeyboardButton("✖️ 关闭", callback_data="close")],
            ]
            await query.edit_message_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))

            return True

        if data.startswith("rlogctx:"):
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
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
            await query.edit_message_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))

            return True

        if data.startswith("logd:"):
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
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
            await query.edit_message_text(
                text[:4096],
                reply_markup=InlineKeyboardMarkup(buttons)
            )

            return True

        if data.startswith("logp:"):
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
            buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])
            await query.edit_message_text(
                text[:4096],
                reply_markup=InlineKeyboardMarkup(buttons)
            )

            return True

        return False
