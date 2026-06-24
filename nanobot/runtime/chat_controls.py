"""Command and native-control catalog for dashboard chat controls."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ChatCommand:
    name: str
    title: str
    description: str
    category: str
    args_label: str = ""
    placeholder: str = ""
    danger: bool = False
    control: str = "button"
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()

    @property
    def command(self) -> str:
        return f"/{self.name}"


COMMAND_CATALOG: tuple[ChatCommand, ...] = (
    ChatCommand("start", "开始", "Start bot session", "会话"),
    ChatCommand("new", "新对话", "Start a new conversation", "会话", danger=True),
    ChatCommand("clear", "清空历史", "Clear conversation history", "会话", danger=True),
    ChatCommand("stop", "停止任务", "Stop the current task", "会话", danger=True),
    ChatCommand("cancel", "取消交互", "Cancel current interaction", "会话", danger=True),
    ChatCommand("help", "帮助", "Show commands", "会话"),
    ChatCommand("agents", "Agent 状态", "List available agents", "Agent"),
    ChatCommand("addagent", "加入 Agent", "Add agent to chat", "Agent", "Agent 名称", "Harper", control="agent"),
    ChatCommand("removeagent", "移除 Agent", "Remove agent", "Agent", "Agent 名称", "Harper", control="agent"),
    ChatCommand("newagent", "新建 Agent", "Create new agent", "Agent"),
    ChatCommand("editagent", "编辑 Agent", "Edit agent config", "Agent", "Agent 名称", "Harper", control="agent"),
    ChatCommand("setleader", "设置 Leader", "Set/clear leader agent", "Agent", "Agent 名称", "Kirk", control="agent"),
    ChatCommand("order", "发言顺序", "Change agent speaking order", "Agent"),
    ChatCommand("hyperparams", "采样参数", "View/edit sampling params", "设置"),
    ChatCommand("groupchat", "群聊参数", "Group chat settings", "设置"),
    ChatCommand("prompt", "提示词栈", "View/edit/reorder prompts", "设置"),
    ChatCommand("history", "历史设置", "History workflow/settings", "设置"),
    ChatCommand("providers", "提供商", "View providers and models", "模型"),
    ChatCommand("newprovider", "添加提供商", "Add provider", "模型"),
    ChatCommand("editprovider", "编辑提供商", "Edit provider", "模型"),
    ChatCommand("deleteprovider", "删除提供商", "Delete provider", "模型", danger=True),
    ChatCommand("newmodel", "添加模型", "Add model", "模型"),
    ChatCommand("deletemodel", "删除模型", "Delete model", "模型", danger=True),
    ChatCommand("speedtest", "测速", "Provider speed test", "模型"),
    ChatCommand("log", "日志", "View session log", "日志", "数量", "20", control="range", min_value=1, max_value=100, step=1),
    ChatCommand("summary", "总结", "Show current summary", "日志"),
    ChatCommand("debug", "调试", "Show debug info", "日志"),
    ChatCommand("groups", "群组列表", "List saved groups", "群组"),
    ChatCommand("savegroup", "保存群组", "Save current members as group", "群组", "群组名", "core", control="text"),
    ChatCommand("loadgroup", "载入群组", "Load saved group", "群组", "群组名", "core", control="text"),
    ChatCommand("delgroup", "删除群组", "Delete saved group", "群组", "群组名", "core", danger=True, control="text"),
    ChatCommand("restart", "重启系统", "Hard reset system", "系统", danger=True),
)


def command_catalog() -> dict:
    categories: list[str] = []
    for item in COMMAND_CATALOG:
        if item.category not in categories:
            categories.append(item.category)
    return {
        "categories": categories,
        "commands": [asdict(item) | {"command": item.command} for item in COMMAND_CATALOG],
    }


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_nested(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_nested(data: dict, dotted: str, value: Any) -> None:
    cur = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _field(
    key: str,
    label: str,
    kind: str,
    *,
    value: Any = None,
    min_value: float | None = None,
    max_value: float | None = None,
    step: float | None = None,
    options: list[str] | None = None,
    action: str = "set_config",
    config: str = "",
) -> dict:
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "value": value,
        "min": min_value,
        "max": max_value,
        "step": step,
        "options": options or [],
        "action": action,
        "config": config,
    }


def control_schema(home: Path | None = None) -> dict:
    """Return native dashboard controls for command submodules."""
    root = home or Path.home() / ".nanobot"
    active = _read_json(root / "active_agents.json", [])
    agents_dir = root / "agents"
    agents = sorted([p.name.title() for p in agents_dir.iterdir() if p.is_dir()]) if agents_dir.is_dir() else []
    leader = (root / "leader.txt").read_text(encoding="utf-8").strip() if (root / "leader.txt").is_file() else ""
    groups = _read_json(root / "groups.json", {})
    hp = _read_json(root / "hyperparams.json", {})
    gc = _read_json(root / "groupchat_settings.json", {})
    hist = _read_json(root / "history_settings.json", {})
    pm = _read_json(root / "providers_models.json", {"providers": {}, "models": {}})
    providers = sorted((pm.get("providers") or {}).keys())

    return {
        "modules": [
            {
                "id": "session",
                "title": "会话",
                "controls": [
                    _field("new", "新对话", "button", action="command", value="/new"),
                    _field("clear", "清空历史", "button", action="command", value="/clear"),
                    _field("stop", "停止任务", "button", action="command", value="/stop"),
                    _field("cancel", "取消交互", "button", action="command", value="/cancel"),
                    _field("help", "帮助", "button", action="command", value="/help"),
                ],
            },
            {
                "id": "agents",
                "title": "Agent",
                "controls": [
                    _field("active", "活跃 Agent", "multiselect", value=active, options=agents, action="agent_active"),
                    _field("leader", "Leader", "select", value=leader, options=[""] + active, action="leader"),
                    _field("agents", "状态", "button", action="command", value="/agents"),
                    _field("newagent", "新建 Agent", "button", action="command", value="/newagent"),
                    _field("editagent", "编辑 Agent", "select_action", value=active[0] if active else "", options=agents, action="command_template", config="/editagent {value}"),
                    _field("order", "发言顺序", "button", action="command", value="/order"),
                ],
            },
            {
                "id": "agent_edit",
                "title": "Agent 子模块",
                "controls": [
                    _field("tools", "工具权限", "button", action="command_template", value=active[0] if active else "", options=agents, config="/editagent {value}"),
                    _field("model", "模型/提供商", "button", action="command_template", value=active[0] if active else "", options=agents, config="/editagent {value}"),
                    _field("rank", "等级", "button", action="command_template", value=active[0] if active else "", options=agents, config="/editagent {value}"),
                    _field("reasoning", "思考深度", "button", action="command_template", value=active[0] if active else "", options=agents, config="/editagent {value}"),
                ],
            },
            {
                "id": "hyperparams",
                "title": "采样参数",
                "controls": [
                    _field("temperature", "temperature", "slider", value=hp.get("temperature", 0.2), min_value=0, max_value=2, step=0.05, config="hyperparams"),
                    _field("top_p", "top_p", "slider", value=hp.get("top_p", 1.0), min_value=0, max_value=1, step=0.01, config="hyperparams"),
                    _field("top_k", "top_k", "number", value=hp.get("top_k", 30), min_value=0, max_value=200, step=1, config="hyperparams"),
                    _field("frequency_penalty", "frequency penalty", "slider", value=hp.get("frequency_penalty", 0), min_value=-2, max_value=2, step=0.05, config="hyperparams"),
                    _field("presence_penalty", "presence penalty", "slider", value=hp.get("presence_penalty", 0), min_value=-2, max_value=2, step=0.05, config="hyperparams"),
                ],
            },
            {
                "id": "groupchat",
                "title": "群聊参数",
                "controls": [
                    _field("tool_initial", "初始工具额度", "number", value=gc.get("tool_initial", 2), min_value=0, max_value=20, step=1, config="groupchat"),
                    _field("allocate_timeout", "消息分配超时", "slider", value=gc.get("allocate_timeout", 12), min_value=1, max_value=60, step=1, config="groupchat"),
                    _field("call_timeout", "Agent 超时", "slider", value=gc.get("call_timeout", 180), min_value=30, max_value=600, step=10, config="groupchat"),
                    _field("leader_call_timeout", "Leader 超时", "slider", value=gc.get("leader_call_timeout", 240), min_value=30, max_value=900, step=10, config="groupchat"),
                    _field("global_timeout", "整轮超时", "slider", value=gc.get("global_timeout", 1800), min_value=60, max_value=3600, step=60, config="groupchat"),
                ],
            },
            {
                "id": "history",
                "title": "历史/上下文",
                "controls": [
                    _field("context_window_tokens", "上下文窗口", "number", value=hist.get("context_window_tokens", 200000), min_value=8000, max_value=300000, step=1000, config="history"),
                    _field("history.max_messages", "历史消息数", "slider", value=_get_nested(hist, "history.max_messages", 50), min_value=5, max_value=200, step=5, config="history"),
                    _field("history.keep_user_messages", "保留用户消息", "toggle", value=_get_nested(hist, "history.keep_user_messages", True), config="history"),
                    _field("history.history_summarize_enabled", "历史压缩", "toggle", value=_get_nested(hist, "history.history_summarize_enabled", True), config="history"),
                    _field("tool_results.summarize_enabled", "工具结果总结", "toggle", value=_get_nested(hist, "tool_results.summarize_enabled", True), config="history"),
                    _field("context_pruning.keep_recent", "保护最近轮数", "slider", value=_get_nested(hist, "context_pruning.keep_recent", 10), min_value=1, max_value=30, step=1, config="history"),
                ],
            },
            {
                "id": "providers",
                "title": "模型/提供商",
                "controls": _provider_controls(providers, pm),
            },
            {
                "id": "prompts",
                "title": "Prompt",
                "controls": [
                    _field("prompt", "提示词管理", "button", action="command", value="/prompt"),
                    _field("history_panel", "历史管理", "button", action="command", value="/history"),
                ],
            },
            {
                "id": "groups",
                "title": "群组",
                "controls": [
                    _field("group", "群组", "select", value=next(iter(groups), ""), options=sorted(groups), action="noop"),
                    _field("groups", "群组列表", "button", action="command", value="/groups"),
                    _field("loadgroup", "载入群组", "select_action", value=next(iter(groups), ""), options=sorted(groups), action="command_template", config="/loadgroup {value}"),
                    _field("savegroup", "保存当前群组", "text_action", value="", action="command_template", config="/savegroup {value}"),
                    _field("delgroup", "删除群组", "select_action", value=next(iter(groups), ""), options=sorted(groups), action="command_template", config="/delgroup {value}"),
                ],
            },
            {
                "id": "logs",
                "title": "日志",
                "controls": [
                    _field("log", "日志条数", "slider_action", value=20, min_value=1, max_value=100, step=1, action="command_template", config="/log {value}"),
                    _field("summary", "总结", "button", action="command", value="/summary"),
                    _field("debug", "调试", "button", action="command", value="/debug"),
                ],
            },
            {
                "id": "system",
                "title": "系统",
                "controls": [
                    _field("restart", "重启", "button", action="command", value="/restart"),
                ],
            },
        ],
    }


def _mask_key_hint(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


def _provider_controls(providers: list[str], pm: dict) -> list[dict]:
    selected = providers[0] if providers else ""
    info = (pm.get("providers") or {}).get(selected, {}) if selected else {}
    return [
        _field(
            "providers",
            "提供商",
            "select",
            value=selected,
            options=providers,
            action="provider_select",
        ),
        _field(
            "provider_url",
            "API 地址",
            "text",
            value=info.get("url", "") if isinstance(info, dict) else "",
            action="noop",
        ),
        _field(
            "provider_key",
            "API Key",
            "password",
            value="",
            action="noop",
        ),
        _field(
            "provider_key_hint",
            "当前 Key",
            "hint",
            value=_mask_key_hint(info.get("apiKey") if isinstance(info, dict) else None),
            action="noop",
        ),
        _field("provider_save", "保存", "button", action="provider_save"),
        _field("providers_panel", "列表", "button", action="command", value="/providers"),
        _field("newprovider", "添加", "button", action="command", value="/newprovider"),
        _field("newmodel", "添加模型", "button", action="command", value="/newmodel"),
        _field("speedtest", "测速", "button", action="command", value="/speedtest"),
    ]


def providers_panel(home: Path | None = None) -> dict:
    """Return editable provider rows for the dashboard."""
    root = home or Path.home() / ".nanobot"
    pm = _read_json(root / "providers_models.json", {"providers": {}, "models": {}})
    rows: list[dict] = []
    for name in sorted((pm.get("providers") or {}).keys()):
        info = pm["providers"].get(name) or {}
        if not isinstance(info, dict):
            continue
        models = pm.get("models", {}).get(name, [])
        model_count = len([
            model for model in models
            if isinstance(model, str) and model.strip() and not model.strip().startswith("══")
        ]) if isinstance(models, list) else 0
        rows.append({
            "name": name,
            "url": info.get("url", ""),
            "key_hint": _mask_key_hint(info.get("apiKey")),
            "model_count": model_count,
        })
    return {"providers": rows}


_CONFIG_FILES = {
    "hyperparams": "hyperparams.json",
    "groupchat": "groupchat_settings.json",
    "history": "history_settings.json",
}


def apply_control_action(body: dict, home: Path | None = None) -> dict:
    root = home or Path.home() / ".nanobot"
    action = str(body.get("action", ""))
    if action == "set_config":
        config = str(body.get("config", ""))
        key = str(body.get("key", ""))
        if config not in _CONFIG_FILES or not key:
            raise ValueError("invalid config action")
        path = root / _CONFIG_FILES[config]
        data = _read_json(path, {})
        _set_nested(data, key, body.get("value"))
        _write_json(path, data)
        return {"ok": True, "message": f"{config}.{key} saved"}
    if action == "agent_active":
        agents = [str(x) for x in body.get("value", []) if str(x)]
        _write_json(root / "active_agents.json", agents)
        return {"ok": True, "message": "active agents saved", "reload": True}
    if action == "leader":
        leader = str(body.get("value", "")).strip()
        (root / "leader.txt").write_text(leader, encoding="utf-8")
        return {"ok": True, "message": "leader saved", "reload": True}
    if action == "provider_save":
        provider = str(body.get("provider", "")).strip()
        if not provider:
            raise ValueError("provider is required")
        url = str(body.get("url", "")).strip()
        api_key = str(body.get("api_key", body.get("apiKey", ""))).strip()
        pm = _read_json(root / "providers_models.json", {"providers": {}, "models": {}})
        providers = pm.setdefault("providers", {})
        models = pm.setdefault("models", {})
        entry = dict(providers.get(provider) or {}) if isinstance(providers.get(provider), dict) else {}
        if url:
            entry["url"] = url
        if api_key:
            entry["apiKey"] = api_key
        if not entry.get("url"):
            raise ValueError("API 地址不能为空")
        providers[provider] = entry
        models.setdefault(provider, models.get(provider, []))
        _write_json(root / "providers_models.json", pm)
        return {"ok": True, "message": f"已保存 {provider}", "reload": True}
    if action in {"noop", "provider_select", ""}:
        return {"ok": True}
    raise ValueError("unsupported action")


def runtime_control_commands(body: dict, home: Path | None = None) -> list[str]:
    """Translate native controls into existing nanobot runtime commands."""
    root = home or Path.home() / ".nanobot"
    action = str(body.get("action", ""))
    if action == "agent_active":
        before = [str(x) for x in _read_json(root / "active_agents.json", []) if str(x)]
        after = [str(x) for x in body.get("value", []) if str(x)]
        before_set = set(before)
        after_set = set(after)
        commands: list[str] = []
        for name in after:
            if name not in before_set:
                commands.append(f"/addagent {name}")
        for name in before:
            if name not in after_set:
                commands.append(f"/removeagent {name}")
        return commands
    if action == "leader":
        leader = str(body.get("value", "")).strip()
        return [f"/setleader {leader}".strip()]
    return []
