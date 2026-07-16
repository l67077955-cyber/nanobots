"""Telegram **settings** UI for context/history knobs (command: /history).

This is the channel **control panel**, NOT live chat bubble rendering.
Live multi-agent conversation UI lives in ``groupchat.display``
(BroadcastView / StreamingDisplay / status_tracker).

Game-style groups: 记忆范围 / 压缩策略 / 工具限制 / 跨轮可见性 / 全局.
Organised by user-facing experience, not internal function names.
Reads/writes ``groupchat.context`` (history_settings, etc.).
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# SmartSearchTool hardcodes this; not read from history_settings yet.
_SMART_SEARCH_SUMMARIZE_THRESHOLD_FALLBACK = 3000

# ── Group definitions ────────────────────────────────────────────────────
# Each group maps a user-facing category to its parameters.
# "params"   = always-visible parameters (high frequency)
# "toggles"  = boolean toggle parameters
# "advanced" = collapsible low-frequency parameters

GROUPS: dict[str, dict[str, Any]] = {
    "memory": {
        "icon": "🧠",
        "title": "记忆范围",
        "subtitle": "对话能聊多久不丢东西",
        "params": [
            ("history", "max_messages"),
            ("context_pruning", "keep_recent"),
            ("history", "compress_ratio"),
            ("history", "compression_keep_recent"),
        ],
        "toggles": [
            ("history", "keep_user_messages"),
            ("history", "history_summarize_enabled"),
            ("history", "cross_turn_repeat_guard"),
        ],
        "advanced": [
            ("history", "compress_max_summary_tokens"),
            ("history", "token_trigger_ratio"),
            ("history", "context_budget_ratio"),
            ("history", "compress_fallback_chars"),
            ("history", "cross_turn_repeat_ratio"),
            ("history", "max_context_chars"),
            ("context_pruning", "soft_ratio"),
        ],
    },
    "compress": {
        "icon": "📦",
        "title": "压缩策略",
        "subtitle": "长内容怎么处理 (截断 / AI压缩)",
        "params": [
            ("tool_results", "exec_max_chars"),
            ("tool_results", "web_search_max_chars"),
            ("tool_results", "web_fetch_max_chars"),
            ("tool_results", "summarize_threshold"),
            ("tool_results", "summarize_model"),
        ],
        "toggles": [
            ("tool_results", "summarize_enabled"),
        ],
        "advanced": [
            ("tool_results", "summarize_max_input_chars"),
            ("tool_results", "summarize_max_output_chars"),
            ("tool_results", "broadcast_result_max_chars"),
            ("tool_results", "direct_result_max_chars"),
            ("__top__", "tool_result_max_chars"),
            ("context_pruning", "soft_max_chars"),
        ],
    },
    "tools": {
        "icon": "🔧",
        "title": "工具限制",
        "subtitle": "工具本身的行为上限",
        "params": [
            ("tool_limits", "read_file_max_chars"),
            ("tool_limits", "read_file_default_lines"),
            ("tool_limits", "exec_max_output"),
            ("tool_limits", "exec_max_timeout"),
            ("tool_limits", "list_dir_default_max"),
        ],
        "toggles": [],
        "advanced": [],
    },
    "vis": {
        "icon": "👁️",
        "title": "跨轮可见性",
        "subtitle": "模型下一轮能看到多少之前的操作",
        "params": [
            ("tool_log_preview", "read_file"),
            ("tool_log_preview", "web_search"),
            ("tool_log_preview", "web_fetch"),
            ("tool_log_preview", "exec"),
            ("tool_log_preview", "_total_cap"),
        ],
        "toggles": [],
        "advanced": [
            ("tool_log_preview", "list_dir"),
            ("tool_log_preview", "write_file"),
            ("tool_log_preview", "edit_file"),
            ("tool_log_preview", "chatroom_send"),
            ("tool_log_preview", "wait"),
            ("tool_log_preview", "_default"),
        ],
    },
    "global": {
        "icon": "🌐",
        "title": "全局",
        "subtitle": "顶层锚点参数",
        "params": [
            ("__top__", "context_window_tokens"),
        ],
        "toggles": [],
        "advanced": [],
    },
}

GROUP_ORDER = ["memory", "compress", "tools", "vis", "global"]

# ── Compact labels for button / display text ─────────────────────────────

_PARAM_LABELS: dict[tuple[str, str], str] = {
    ("__top__", "context_window_tokens"): "上下文窗口",
    ("__top__", "tool_result_max_chars"): "工具结果fallback",
    ("tool_results", "exec_max_chars"): "exec截断",
    ("tool_results", "web_fetch_max_chars"): "fetch截断",
    ("tool_results", "web_search_max_chars"): "search截断",
    ("tool_results", "summarize_enabled"): "AI压缩",
    ("tool_results", "summarize_threshold"): "压缩阈值",
    ("tool_results", "summarize_model"): "压缩模型",
    ("tool_results", "summarize_max_input_chars"): "最大输入",
    ("tool_results", "summarize_max_output_chars"): "最大输出",
    ("tool_results", "broadcast_result_max_chars"): "广播模式",
    ("tool_results", "direct_result_max_chars"): "直接模式",
    ("history", "max_messages"): "最大消息数",
    ("history", "max_context_chars"): "字符上限",
    ("history", "compress_ratio"): "压缩触发比",
    ("history", "compress_max_summary_tokens"): "摘要tokens",
    ("history", "token_trigger_ratio"): "token触发比",
    ("history", "context_budget_ratio"): "预算占比",
    ("history", "compress_fallback_chars"): "回退压缩字数",
    ("history", "cross_turn_repeat_guard"): "跨轮重复守卫",
    ("history", "cross_turn_repeat_ratio"): "跨轮重复阈值",
    ("history", "compression_keep_recent"): "尾保条数",
    ("history", "keep_user_messages"): "保护用户消息",
    ("history", "history_summarize_enabled"): "历史AI压缩",
    ("context_pruning", "soft_ratio"): "软裁剪比例",
    ("context_pruning", "keep_recent"): "保留最近",
    ("context_pruning", "soft_max_chars"): "软裁剪阈值",
    ("tool_limits", "read_file_max_chars"): "文件读取上限",
    ("tool_limits", "read_file_default_lines"): "文件默认行数",
    ("tool_limits", "list_dir_default_max"): "目录条目上限",
    ("tool_limits", "exec_max_timeout"): "命令超时",
    ("tool_limits", "exec_max_output"): "命令输出上限",
    ("tool_log_preview", "read_file"): "read预览",
    ("tool_log_preview", "web_search"): "search预览",
    ("tool_log_preview", "web_fetch"): "fetch预览",
    ("tool_log_preview", "exec"): "exec预览",
    ("tool_log_preview", "list_dir"): "list_dir预览",
    ("tool_log_preview", "write_file"): "write预览",
    ("tool_log_preview", "edit_file"): "edit预览",
    ("tool_log_preview", "chatroom_send"): "chatroom预览",
    ("tool_log_preview", "wait"): "wait预览",
    ("tool_log_preview", "_default"): "默认预览",
    ("tool_log_preview", "_total_cap"): "总上限",
}


def _param_label(section: str, key: str) -> str:
    return _PARAM_LABELS.get((section, key), f"{section}.{key}")


def _get_val(settings: dict[str, Any], section: str, key: str) -> Any:
    if section == "__top__":
        return settings.get(key)
    return settings.get(section, {}).get(key)


def _format_val(val: Any) -> str:
    if isinstance(val, bool):
        return "✅" if val else "❌"
    if isinstance(val, int):
        return f"{val:,}"
    return str(val)


def find_group_for_param(section: str, key: str) -> str:
    """Reverse-lookup which group owns a (section, key) pair."""
    for group_id, g in GROUPS.items():
        for s, k in g["params"] + g["toggles"] + g["advanced"]:
            if s == section and k == key:
                return group_id
    return "memory"


# ── Settings / engine helpers (unchanged) ────────────────────────────────

def _settings() -> dict[str, Any]:
    from nanobot.groupchat.context import history_settings as hs
    return hs.get_all()


def _history_messages(engine: Any | None) -> list[dict[str, Any]]:
    if not engine:
        return []
    return list(engine.history.to_sender_dicts())


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
    from nanobot.core.history import History

    messages = _history_messages(engine)
    parts: list[str] = []
    for agent in engine._active_agents:
        try:
            compiled = History.from_sender_dicts(messages).build_for_groupchat(current_agent=agent)
            chars = sum(len(m.get("content") or "") for m in compiled)
            parts.append(f"{agent}~{chars:,}字")
        except Exception:
            parts.append(f"{agent}:?")
    return " | ".join(parts) if parts else "(无活跃agent)"


# ── Live metrics (unchanged) ─────────────────────────────────────────────

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
    token_trigger_ratio = float(hist.get("token_trigger_ratio", 0.55))

    current_tok = _estimate_history_tokens(messages)
    msg_pct = int(current_msgs / max(1, max_msgs) * 100)
    char_pct = int(current_chars / max(1, max_chars) * 100) if max_chars else 0
    tok_pct = int(current_tok / max(1, ctx_window) * 100) if ctx_window else 0
    compress_trigger = int(max_msgs * compress_ratio)
    compress_msg_pct = int(current_msgs / max(1, max_msgs) * 100)
    # Mirror maybe_compress's actual trigger: effective_ratio >= compress_ratio
    # OR token_ratio >= token_trigger_ratio. Previously the token leg was a
    # hardcoded `tok_pct >= 55` — a fourth copy of the 0.55 magic number that
    # drifted from the real (now configurable) threshold.
    compress_ready = (
        max(compress_msg_pct, tok_pct) >= int(compress_ratio * 100)
        or tok_pct >= int(token_trigger_ratio * 100)
    )

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
    # The pipeline runs AI summarization (step 1.5) BEFORE truncation (step 2),
    # so a threshold above the per-tool cap does NOT disable AI compression —
    # large results still get summarized first. The old "AI压缩永远不生效"
    # warning was backwards and fired on the recommended default. Only warn
    # when AI compression is effectively unreachable: threshold so high no
    # plausible result would reach it while truncation cap is far below.
    if threshold > 0 and per_tool_max > 0 and threshold > per_tool_max * 4:
        warnings.append(
            f"压缩阈值({threshold:,}) 远超工具截断上限({per_tool_max:,})"
            " — 介于截断上限与压缩阈值之间的结果会被截断(落盘)而非AI压缩"
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


# ── Dashboard block (simplified: status, not parameters) ────────────────

def _dashboard_block(metrics: dict[str, Any]) -> str:
    if metrics["compress_ready"]:
        compress_state = "🔴 已达触发线"
    else:
        compress_state = "🟢 安全"

    warnings = collect_config_warnings(metrics["settings"])
    warn_lines = ""
    if warnings:
        warn_lines = "\n⚠ 配置告警\n" + "\n".join(f"  • {w}" for w in warnings[:3])
        if len(warnings) > 3:
            warn_lines += f"\n  • …另有 {len(warnings) - 3} 条"

    bar_filled = min(20, metrics["tok_pct"] * 20 // 100)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)

    remaining_tok = max(0, metrics["ctx_window"] - metrics["current_tok"])
    remaining_rounds = remaining_tok // 700 if remaining_tok > 0 else 0
    round_hint = f"  预计还可聊 ~{remaining_rounds} 轮" if remaining_rounds > 0 and "安全" in compress_state else ""

    return (
        "━━━ 实时状态 ━━━\n"
        f"容量  {bar} {metrics['tok_pct']}%\n"
        f"消息  {metrics['current_msgs']}/{metrics['max_msgs']}条"
        f"  ~{metrics['current_tok']:,}/{metrics['ctx_window']:,}tok\n"
        f"状态  {compress_state}{round_hint}\n"
        f"编译  {metrics['compiled_info']}"
        + warn_lines
    )


# ── Grouped overview (replaces _pipeline_summary) ────────────────────────

def _grouped_overview(metrics: dict[str, Any]) -> str:
    settings = metrics["settings"]
    hist = metrics["history"]
    cp = metrics["context_pruning"]
    tr = metrics["tool_results"]
    tl = settings.get("tool_limits", {})
    pv = settings.get("tool_log_preview", {})

    keep_users = "全部用户" if hist.get("keep_user_messages") else "首条用户"
    ai_on = "✅" if tr.get("summarize_enabled") else "❌"
    hist_ai = "✅" if hist.get("history_summarize_enabled", True) else "❌"

    return (
        "━━━ 分组概览 ━━━\n"
        f"🧠 记忆范围   {hist['max_messages']}条/保{cp.get('keep_recent', 4)}轮"
        f"/尾保{hist.get('compression_keep_recent', 6)}条/{keep_users}\n"
        f"📦 压缩策略   exec={tr['exec_max_chars']:,}"
        f" AI{ai_on} 阈值{tr['summarize_threshold']:,}"
        f" 历史AI{hist_ai}\n"
        f"🔧 工具限制   read={tl.get('read_file_max_chars', 64000):,}"
        f" exec={tl.get('exec_max_output', 10000):,}/{tl.get('exec_max_timeout', 600)}s\n"
        f"👁️ 跨轮可见性 cap={pv.get('_total_cap', 4000):,}"
        f" read={pv.get('read_file', 1500):,}\n"
        f"🌐 全局       ctx={settings['context_window_tokens']:,}tok"
    )


# ── Flow demo (replaces _pipeline_demo_expanded, no S1-S6 numbering) ─────

def _flow_demo(metrics: dict[str, Any]) -> str:
    settings = metrics["settings"]
    tr = metrics["tool_results"]
    hist = metrics["history"]
    cp = metrics["context_pruning"]
    tl = settings.get("tool_limits", {})
    pv = settings.get("tool_log_preview", {})

    return (
        "━━━ 管线流程 ━━━\n"
        "工具执行 → 结果处理 → 存入历史 → LLM调用前裁剪 → 下一轮预览\n\n"
        "① 工具内截断 (工具自身)\n"
        f"   exec → head_tail @{tl.get('exec_max_output', 10000):,}\n"
        f"   read_file → @{tl.get('read_file_max_chars', 64000):,}\n\n"
        "② 结果后处理 (process_tool_result)\n"
        f"   AI压缩({'✅' if tr['summarize_enabled'] else '❌'} 阈值{tr['summarize_threshold']:,})"
        f" → 截断(exec head_tail@{tr['exec_max_chars']:,}"
        f" / web head_only@{tr['web_search_max_chars']:,})"
        f" → 落盘 + meta\n\n"
        "③ 存入历史 (add_message)\n"
        f"   超 {hist['max_messages']}条 或 token > {metrics['ctx_window']:,}×{int(hist.get('context_budget_ratio', 0.65) * 100)}% → 丢弃最旧\n"
        f"   头保护: {'全部用户' if hist.get('keep_user_messages') else '首条用户'}\n\n"
        "④ 历史压缩 (maybe_compress)\n"
        f"   触发: msg≥{int(hist.get('compress_ratio', 0.8) * 100)}% 或 tok≥{int(hist.get('token_trigger_ratio', 0.55) * 100)}%\n"
        f"   中间段 → {tr['summarize_model']}"
        f" (尾保{hist.get('compression_keep_recent', 6)}条)\n"
        f"   AI压缩({'✅' if hist.get('history_summarize_enabled', True) else '❌'})\n\n"
        "⑤ 迭代裁剪 (prune_messages, tool_loop iter≥2)\n"
        f"   tok/窗口≥{cp.get('soft_ratio', 0.55)}"
        f" → 旧tool一行摘要 (保{cp.get('keep_recent', 4)}轮)\n\n"
        "⑥ 下一轮预览 (build_tool_log)\n"
        f"   read={pv.get('read_file', 1500):,}"
        f" exec={pv.get('exec', 500):,}"
        f" 总cap={pv.get('_total_cap', 4000):,}\n"
    )


# ── Main panel ───────────────────────────────────────────────────────────

def build_main_panel_text(engine: Any | None, *, expanded: bool = False) -> str:
    metrics = collect_live_metrics(engine)
    parts = [_dashboard_block(metrics), _grouped_overview(metrics)]
    if expanded:
        parts.append(_flow_demo(metrics))
    return "\n\n".join(parts)


def build_main_panel_buttons(
    engine: Any | None,
    *,
    expanded: bool = False,
) -> InlineKeyboardMarkup:
    metrics = collect_live_metrics(engine)

    buttons: list[list[InlineKeyboardButton]] = []
    for group_id in GROUP_ORDER:
        g = GROUPS[group_id]
        summary = _group_summary_line(group_id, metrics)
        label = f"{g['icon']} {g['title']}  {summary}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"hs_grp:{group_id}")])

    demo_label = "📋 收起流程" if expanded else "📖 管线流程"
    demo_cb = "hs_demo:0" if expanded else "hs_demo:1"
    buttons.append([InlineKeyboardButton(demo_label, callback_data=demo_cb)])
    buttons.append([InlineKeyboardButton("🔄 重载配置", callback_data="hs_reload")])
    buttons.append([InlineKeyboardButton("↩️ 恢复全部默认", callback_data="hs_rst")])
    buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])

    return InlineKeyboardMarkup(buttons)


def _group_summary_line(group: str, metrics: dict[str, Any]) -> str:
    settings = metrics["settings"]
    if group == "memory":
        hist = metrics["history"]
        cp = metrics["context_pruning"]
        keep_users = "全部用户" if hist.get("keep_user_messages") else "首条用户"
        return (f"{hist['max_messages']}条/保{cp.get('keep_recent', 4)}轮"
                f"/尾保{hist.get('compression_keep_recent', 6)}条/{keep_users}")
    elif group == "compress":
        tr = metrics["tool_results"]
        ai = "AI✅" if tr.get("summarize_enabled") else "AI❌"
        return f"exec={tr['exec_max_chars']:,} {ai} 阈值{tr['summarize_threshold']:,}"
    elif group == "tools":
        tl = settings.get("tool_limits", {})
        return (f"read={tl.get('read_file_max_chars', 64000):,}"
                f" exec={tl.get('exec_max_output', 10000):,}")
    elif group == "vis":
        pv = settings.get("tool_log_preview", {})
        return f"cap={pv.get('_total_cap', 4000):,} read={pv.get('read_file', 1500):,}"
    elif group == "global":
        return f"ctx={settings['context_window_tokens']:,}tok"
    return ""


# ── Group sub-panel builder ──────────────────────────────────────────────

def build_group_panel(
    engine: Any | None,
    group: str,
    *,
    advanced: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a sub-panel for a specific parameter group."""
    if group not in GROUPS:
        group = "memory"

    g = GROUPS[group]
    settings = _settings()

    # ── Text ──
    lines: list[str] = [f"{g['icon']} {g['title']}", g["subtitle"], ""]

    # Live metrics for memory group
    if group == "memory":
        metrics = collect_live_metrics(engine)
        if metrics["compress_ready"]:
            state = "🔴 已达触发线"
        else:
            state = "🟢 安全"
        lines.append("━━ 实时 ━━")
        lines.append(f"  {metrics['current_msgs']}/{metrics['max_msgs']}条"
                     f"  ~{metrics['current_tok']:,}tok ({metrics['tok_pct']}%)")
        lines.append(f"  状态  {state}")
        lines.append("")

    lines.append("━━ 参数 ━━")
    for section, key in g["params"]:
        val = _get_val(settings, section, key)
        label = _param_label(section, key)
        lines.append(f"  {label:<14}  {_format_val(val)}")

    for section, key in g["toggles"]:
        val = _get_val(settings, section, key)
        label = _param_label(section, key)
        lines.append(f"  {label:<14}  {'✅' if val else '❌'}")

    if advanced and g["advanced"]:
        lines.append("\n━━ 高级 ━━")
        for section, key in g["advanced"]:
            val = _get_val(settings, section, key)
            label = _param_label(section, key)
            lines.append(f"  {label:<14}  {_format_val(val)}")

    text = "\n".join(lines)

    # ── Buttons ──
    buttons: list[list[InlineKeyboardButton]] = []

    for section, key in g["params"]:
        val = _get_val(settings, section, key)
        label = _param_label(section, key)
        buttons.append([InlineKeyboardButton(
            f"✏️ {label}: {_format_val(val)}",
            callback_data=f"hs_edit:{section}:{key}",
        )])

    for section, key in g["toggles"]:
        val = _get_val(settings, section, key)
        label = _param_label(section, key)
        if val:
            buttons.append([InlineKeyboardButton(
                f"❌ 关闭 {label}",
                callback_data=f"hs_set:{section}:{key}:false",
            )])
        else:
            buttons.append([InlineKeyboardButton(
                f"✅ 开启 {label}",
                callback_data=f"hs_set:{section}:{key}:true",
            )])

    if g["advanced"]:
        adv_label = "▽ 收起高级" if advanced else "▸ 高级设置"
        adv_cb = f"hs_adv:{group}:0" if advanced else f"hs_adv:{group}:1"
        buttons.append([InlineKeyboardButton(adv_label, callback_data=adv_cb)])

    if advanced:
        for section, key in g["advanced"]:
            val = _get_val(settings, section, key)
            label = _param_label(section, key)
            buttons.append([InlineKeyboardButton(
                f"✏️ {label}: {_format_val(val)}",
                callback_data=f"hs_edit:{section}:{key}",
            )])

    buttons.append([InlineKeyboardButton(f"↩️ 恢复此组默认", callback_data=f"hs_rst:{group}")])
    buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data="hs_back")])
    buttons.append([InlineKeyboardButton("✖️ 关闭", callback_data="close")])

    return text, InlineKeyboardMarkup(buttons)


# ── Restore defaults ─────────────────────────────────────────────────────

def restore_defaults(group: str | None = None) -> str:
    """Restore defaults for all groups or a specific group.

    Returns a confirmation message.
    """
    from nanobot.groupchat.context import history_settings as hs
    import json

    defaults = json.loads(json.dumps(hs._DEFAULTS))

    if group is None or group == "all":
        hs.save(defaults)
        return "✅ 已恢复全部默认设置"
    elif group in GROUPS:
        settings = hs.get_all()
        g = GROUPS[group]
        for section, key in g["params"] + g["toggles"] + g["advanced"]:
            if section == "__top__":
                if key in defaults:
                    settings[key] = defaults[key]
            else:
                if section in defaults and key in defaults[section]:
                    settings.setdefault(section, {})[key] = defaults[section][key]
        hs.save(settings)
        return f"✅ 已恢复 {GROUPS[group]['title']} 默认设置"
    return "❌ 未知的分组"


# ── Public API ───────────────────────────────────────────────────────────

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
    """Backward-compat wrapper — delegates to memory group panel."""
    return build_group_panel(engine, "memory")
