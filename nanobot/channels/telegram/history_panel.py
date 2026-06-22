"""Shared builder for the /history Telegram settings panel."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# SmartSearchTool hardcodes this; not read from history_settings yet.
_SMART_SEARCH_SUMMARIZE_THRESHOLD = 3000


def _settings() -> dict[str, Any]:
    from nanobot.groupchat.history import history_settings as hs

    return hs.get_all()


def _history_messages(engine: Any | None) -> list[dict[str, Any]]:
    if not engine:
        return []
    history = getattr(engine, "_history", None) or []
    return list(history)


def _estimate_history_tokens(messages: list[dict[str, Any]]) -> int:
    if not messages:
        return 0
    try:
        from nanobot.utils.helpers import estimate_message_tokens

        def _as_llm(m: dict[str, Any]) -> dict[str, str]:
            role = "user" if m.get("sender") in ("User", "user", "用户") else "assistant"
            return {"role": role, "content": m.get("content", "")}

        return sum(int(estimate_message_tokens(_as_llm(m)) or 0) for m in messages)
    except Exception:
        return sum(len(m.get("content", "")) for m in messages) // 4


def _compiled_context_info(engine: Any | None) -> str:
    if not engine or not getattr(engine, "_active_agents", None):
        return "(engine未启动)"
    from nanobot.groupchat.history.prompt_builder import PromptBuilder

    messages = _history_messages(engine)
    parts: list[str] = []
    for agent in engine._active_agents:
        try:
            compiled = PromptBuilder.history_to_messages(messages, current_agent=agent)
            chars = sum(len(m.get("content") or "") for m in compiled)
            parts.append(f"{agent}~{chars:,}字")
        except Exception:
            parts.append(f"{agent}:?")
    return " | ".join(parts) if parts else "(无活跃agent)"


def collect_live_metrics(engine: Any | None) -> dict[str, Any]:
    """Gather runtime numbers used by the /history dashboard."""
    settings = _settings()
    tr = settings["tool_results"]
    hist = settings["history"]
    cp = settings.get("context_pruning", {})
    messages = _history_messages(engine)

    current_msgs = len(messages)
    current_chars = sum(len(m.get("content", "")) for m in messages)
    max_msgs = int(hist["max_messages"])
    max_chars = int(hist["max_context_chars"])
    ctx_window = int(settings["context_window_tokens"])
    compress_ratio = float(hist.get("compress_ratio", 0.8))
    soft_ratio = float(cp.get("soft_ratio", 0.55))

    current_tok = _estimate_history_tokens(messages)
    msg_pct = int(current_msgs / max(1, max_msgs) * 100)
    char_pct = int(current_chars / max(1, max_chars) * 100) if max_chars else 0
    tok_pct = int(current_tok / max(1, ctx_window) * 100) if ctx_window else 0
    compress_trigger = int(max_msgs * compress_ratio)
    compress_msg_pct = int(current_msgs / max(1, max_msgs) * 100)
    compress_ready = max(compress_msg_pct, tok_pct) >= int(compress_ratio * 100) or tok_pct >= 55

    compress_warned = False
    if engine is not None and getattr(engine, "history", None) is not None:
        compress_warned = bool(getattr(engine.history, "_compress_warned", False))

    return {
        "settings": settings,
        "tool_results": tr,
        "history": hist,
        "context_pruning": cp,
        "current_msgs": current_msgs,
        "current_chars": current_chars,
        "current_tok": current_tok,
        "max_msgs": max_msgs,
        "max_chars": max_chars,
        "ctx_window": ctx_window,
        "msg_pct": msg_pct,
        "char_pct": char_pct,
        "tok_pct": tok_pct,
        "compress_trigger": compress_trigger,
        "compress_ratio": compress_ratio,
        "compress_ready": compress_ready,
        "compress_warned": compress_warned,
        "soft_ratio": soft_ratio,
        "compiled_info": _compiled_context_info(engine),
    }


def collect_config_warnings(settings: dict[str, Any] | None = None) -> list[str]:
    """Detect settings that disagree with the real algorithm or each other."""
    if settings is None:
        settings = _settings()
    tr = settings["tool_results"]
    warnings: list[str] = []

    per_tool_max = min(
        int(tr.get("exec_max_chars", 0)),
        int(tr.get("web_fetch_max_chars", 0)),
        int(tr.get("web_search_max_chars", 0)),
    )
    threshold = int(tr.get("summarize_threshold", 0))
    if threshold > per_tool_max:
        warnings.append(
            f"summarize_threshold({threshold:,}) > 最小工具上限({per_tool_max:,})"
            " — 配置阈值对 exec/search/fetch 无效"
        )

    warnings.append(
        "tool_results.summarize_* 未接入通用管线"
        f" — 实际仅 SmartSearch 硬编码 {_SMART_SEARCH_SUMMARIZE_THRESHOLD:,} 字符"
    )
    warnings.append(
        "broadcast/direct_result_max_chars 未接入 tool_loop 注入路径"
        " — 目前只影响 dedup 缓存"
    )
    if int(settings.get("tool_result_max_chars", 0)) < per_tool_max:
        warnings.append(
            f"tool_result_max_chars({settings['tool_result_max_chars']:,})"
            f" < 最小 per-tool 上限({per_tool_max:,}) — 仅作未知工具 fallback"
        )

    extra_keys = set(tr) - {
        "exec_max_chars",
        "web_fetch_max_chars",
        "web_search_max_chars",
        "summarize_enabled",
        "summarize_threshold",
        "summarize_model",
        "summarize_max_input_chars",
        "summarize_max_output_chars",
        "broadcast_result_max_chars",
        "direct_result_max_chars",
    }
    for key in sorted(extra_keys):
        warnings.append(f"tool_results.{key} 不在 schema 内 — 当前被忽略")

    return warnings


def _dashboard_block(metrics: dict[str, Any]) -> str:
    hist = metrics["history"]
    cp = metrics["context_pruning"]
    keep_recent = int(hist.get("compression_keep_recent", 6))
    keep_users = bool(hist.get("keep_user_messages", False))
    hist_sum = bool(hist.get("history_summarize_enabled", True))

    if metrics["compress_warned"]:
        compress_state = "⚠️ 已预警，下轮将压缩"
    elif metrics["compress_ready"]:
        compress_state = "🔴 已达触发线"
    else:
        compress_state = "🟢 未触发"

    head_desc = "全部用户消息" if keep_users else "首条用户消息"
    warnings = collect_config_warnings(metrics["settings"])
    warn_lines = "\n".join(f"  • {w}" for w in warnings[:4])
    if len(warnings) > 4:
        warn_lines += f"\n  • …另有 {len(warnings) - 4} 条"

    return (
        "━━━ 实时状态 ━━━\n"
        f"历史缓冲  {metrics['current_msgs']}/{metrics['max_msgs']}条"
        f"  {metrics['current_chars']:,}/{metrics['max_chars']:,}字"
        f"  ~{metrics['current_tok']:,}/{metrics['ctx_window']:,}tok ({metrics['tok_pct']}%)\n"
        f"压缩      msg{metrics['msg_pct']}% tok{metrics['tok_pct']}%"
        f" → 触发≥{int(metrics['compress_ratio'] * 100)}% 或 tok≥55%"
        f"  [{compress_state}]\n"
        f"编译上下文  {metrics['compiled_info']}\n"
        f"裁剪(配置)  触发 tok/窗口 ≥ {int(metrics['soft_ratio'] * 100)}%"
        f"  |  历史tok比 {metrics['tok_pct']}% (tool_loop 内消息另计)\n"
        f"历史压缩    尾保 {keep_recent}条"
        f" | 头保 {head_desc}"
        f" | history_summarize={'✅' if hist_sum else '❌'}\n"
        f"\n⚠ 配置健康\n{warn_lines}\n"
    )


def _pipeline_summary(metrics: dict[str, Any]) -> str:
    settings = metrics["settings"]
    tr = metrics["tool_results"]
    hist = metrics["history"]
    cp = metrics["context_pruning"]
    keep_recent = int(cp.get("keep_recent", 4))
    tail_keep = int(hist.get("compression_keep_recent", 6))

    return (
        "━━━ 管线摘要 (与算法一致) ━━━\n"
        "S1 process_tool_result\n"
        f"   exec=head_tail@{tr['exec_max_chars']:,}"
        f" | web_fetch/web_search=head_only@{tr['web_fetch_max_chars']:,}/{tr['web_search_max_chars']:,}\n"
        "S2 工具AI压缩\n"
        f"   配置 summarize_*={'✅' if tr['summarize_enabled'] else '❌'}"
        f" 阈值{tr['summarize_threshold']:,} — ⚠未接入通用管线\n"
        f"   实际 SmartSearch 硬编码 {_SMART_SEARCH_SUMMARIZE_THRESHOLD:,} 字符\n"
        "S3 maybe_compress (HistoryContext)\n"
        f"   触发 max(msg%,tok%)≥{hist.get('compress_ratio', 0.8)}"
        f" 或 tok≥55% | 模型 {tr['summarize_model']}\n"
        f"   尾保 {tail_keep}条 | history_summarize={'✅' if hist.get('history_summarize_enabled', True) else '❌'}\n"
        "S4 prune_messages (tool_loop iter≥2)\n"
        f"   tok/窗口≥{cp.get('soft_ratio', 0.55)} → 旧 tool 一行摘要"
        f" | 保最近 {keep_recent} 个 assistant 轮\n"
        "丢弃      add_message 超 max_messages / max_context_chars 时从最早丢弃\n"
        f"全局      context_window={settings['context_window_tokens']:,}tok"
        f" | tool_result_max={settings['tool_result_max_chars']:,}"
        " (未知工具 fallback)\n"
    )


def _pipeline_demo_expanded(metrics: dict[str, Any]) -> str:
    settings = metrics["settings"]
    tr = metrics["tool_results"]
    hist = metrics["history"]
    cp = metrics["context_pruning"]
    compress_trigger = metrics["compress_trigger"]
    keep_recent = int(cp.get("keep_recent", 4))
    tail_keep = int(hist.get("compression_keep_recent", 6))
    keep_users = bool(hist.get("keep_user_messages", False))
    head_desc = "全部用户消息" if keep_users else "首条+首条用户消息"
    ai_on = bool(tr["summarize_enabled"])

    return (
        "━━━ 管线详解 ━━━\n"
        "👤 用户: 帮我搜索特朗普的图片并下载\n"
        f" └─ 🛡 头部保护: {head_desc}\n"
        "\n"
        "── 轮次 1 ──\n"
        "🤖 Agent → web_search(...)\n"
        f"📡 返回 12,000 字符 → process_tool_result\n"
        f" └─ head_only @ web_search_max={tr['web_search_max_chars']:,}\n"
        f" └─ ⚠ summarize_threshold={tr['summarize_threshold']:,} 未接线"
        f" | SmartSearch 实际 @{_SMART_SEARCH_SUMMARIZE_THRESHOLD:,}\n"
        "\n"
        "── 轮次 2 ──\n"
        "🤖 Agent → exec(...)\n"
        f"💻 返回 3,000 字符 → head_tail @ exec_max={tr['exec_max_chars']:,} (未截断)\n"
        "\n"
        f"── Stage4 迭代裁剪 (iter≥2) ──\n"
        f" tok/窗口 ≥ {cp.get('soft_ratio', 0.55)} 且 tool result > {cp.get('soft_max_chars', 8000):,}字\n"
        f" → 替换为一行摘要 | 保最近 {keep_recent} 个 assistant 轮\n"
        "\n"
        f"── Stage3 历史压缩 ──\n"
        f" 触发: 条数≥{compress_trigger} 或 tok比≥55% 或 msg/tok ≥ compress_ratio\n"
        f" 中间段 → {tr['summarize_model']} (max {hist.get('compress_max_summary_tokens', 600)} tok)\n"
        f" 尾保 {tail_keep}条 | history_summarize={'✅' if hist.get('history_summarize_enabled', True) else '❌'}\n"
        "\n"
        f"── 超限丢弃 ──\n"
        f" >{hist['max_messages']}条 或 >{hist['max_context_chars']:,}字 → 从最早丢弃\n"
        f"\n(S2 配置开关 summarize_enabled={'✅' if ai_on else '❌'} — 见管线摘要中的未接线说明)\n"
    )


def build_main_panel_text(engine: Any | None, *, expanded: bool = False) -> str:
    metrics = collect_live_metrics(engine)
    parts = [_dashboard_block(metrics), _pipeline_summary(metrics)]
    if expanded:
        parts.append(_pipeline_demo_expanded(metrics))
    return "\n".join(parts)


def build_main_panel_buttons(
    engine: Any | None,
    *,
    expanded: bool = False,
) -> InlineKeyboardMarkup:
    metrics = collect_live_metrics(engine)
    settings = metrics["settings"]
    tr = metrics["tool_results"]
    hist = metrics["history"]
    cp = metrics["context_pruning"]
    compress_trigger = metrics["compress_trigger"]
    ai_on = bool(tr["summarize_enabled"])
    hist_sum = bool(hist.get("history_summarize_enabled", True))

    demo_label = "📋 收起详解" if expanded else "📖 管线详解"
    demo_cb = "hs_demo:0" if expanded else "hs_demo:1"

    buttons = [
        [InlineKeyboardButton(demo_label, callback_data=demo_cb)],
        [
            InlineKeyboardButton(
                f"🌐 全局: ctx={settings['context_window_tokens']:,}tok",
                callback_data="hs_global",
            )
        ],
        [
            InlineKeyboardButton(
                f"✂️ S1截断: exec={tr['exec_max_chars']:,} search={tr['web_search_max_chars']:,}",
                callback_data="hs_stage1",
            )
        ],
        [
            InlineKeyboardButton(
                f"🧠 S2工具AI: {'✅' if ai_on else '❌'}⚠未接线",
                callback_data="hs_stage2",
            )
        ],
        [
            InlineKeyboardButton(
                f"📚 S3历史: {metrics['current_msgs']}/{hist['max_messages']}条"
                f" tok{metrics['tok_pct']}% @{compress_trigger} {'✅' if hist_sum else '❌'}",
                callback_data="hs_stage3",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔪 S4裁剪: soft@{cp.get('soft_ratio', 0.55)} 保{cp.get('keep_recent', 4)}轮",
                callback_data="hs_stage4",
            )
        ],
        [InlineKeyboardButton("🔄 重载配置", callback_data="hs_reload")],
    ]
    return InlineKeyboardMarkup(buttons)


def build_history_panel(
    engine: Any | None,
    *,
    expanded: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Return main /history panel text and inline keyboard."""
    text = build_main_panel_text(engine, expanded=expanded)
    markup = build_main_panel_buttons(engine, expanded=expanded)
    return text, markup


def build_stage3_panel(engine: Any | None) -> tuple[str, InlineKeyboardMarkup]:
    """Stage 3 sub-panel: history storage + compression."""
    metrics = collect_live_metrics(engine)
    hist = metrics["history"]
    tr = metrics["tool_results"]
    keep_users = bool(hist.get("keep_user_messages", False))
    hist_sum = bool(hist.get("history_summarize_enabled", True))
    tail_keep = int(hist.get("compression_keep_recent", 6))

    toggle_users = "❌ 仅首条用户" if keep_users else "✅ 保护全部用户"
    users_val = "false" if keep_users else "true"
    toggle_sum = "❌ 关闭" if hist_sum else "✅ 开启"
    sum_val = "false" if hist_sum else "true"

    text = (
        "📚 Stage 3: 历史存储 & 压缩\n"
        "add_message 超限时丢弃 | maybe_compress 达线时压缩中间段\n\n"
        f"实时  {metrics['current_msgs']}/{hist['max_messages']}条"
        f"  {metrics['current_chars']:,}/{hist['max_context_chars']:,}字"
        f"  ~{metrics['current_tok']:,}tok ({metrics['tok_pct']}%)\n"
        f"压缩  触发≥{int(metrics['compress_ratio'] * 100)}% 或 tok≥55%"
        f"  → @{metrics['compress_trigger']}条"
        f"  [{'已预警' if metrics['compress_warned'] else '正常'}]\n\n"
        f"头保护  {'全部用户消息' if keep_users else '首条用户消息'}\n"
        f"尾保护  最近 {tail_keep} 条\n"
        f"压缩模型  {tr['summarize_model']}\n"
        f"摘要上限  {hist.get('compress_max_summary_tokens', 600)} tokens"
    )
    buttons = [
        [InlineKeyboardButton(f"消息数: {hist['max_messages']}", callback_data="hs_edit:history:max_messages")],
        [InlineKeyboardButton(f"上下文字符: {hist['max_context_chars']:,}", callback_data="hs_edit:history:max_context_chars")],
        [InlineKeyboardButton(f"压缩比例: {hist.get('compress_ratio', 0.8)}", callback_data="hs_edit:history:compress_ratio")],
        [InlineKeyboardButton(f"摘要tokens: {hist.get('compress_max_summary_tokens', 600)}", callback_data="hs_edit:history:compress_max_summary_tokens")],
        [InlineKeyboardButton(f"尾保条数: {tail_keep}", callback_data="hs_edit:history:compression_keep_recent")],
        [InlineKeyboardButton(toggle_users, callback_data=f"hs_set:history:keep_user_messages:{users_val}")],
        [InlineKeyboardButton(f"{toggle_sum} 历史AI压缩", callback_data=f"hs_set:history:history_summarize_enabled:{sum_val}")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")],
    ]
    return text, InlineKeyboardMarkup(buttons)