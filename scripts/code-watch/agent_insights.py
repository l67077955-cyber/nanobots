"""Agent-engineering insights for the code-watch dashboard (stdlib only)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_GROUP_ONLY_COMPONENTS = frozenset({
    "broadcast_hint", "group_context", "group_nudge", "leader_prompt",
})

_AGENT_FILES = (
    ("SOUL.md", "persona", "人设"),
    ("MEMORY.md", "memory", "长期记忆"),
    ("INSTRUCTIONS.md", "instructions", "指令"),
    ("AGENTS.md", "agents", "Agent 说明"),
    ("TOOLS.md", "tools", "工具"),
    ("USER.md", "user", "用户"),
)

_PREVIEW_CHARS = 280
_LOG_TAIL_LINES = 18
_EVENT_TAIL_LINES = 40

MODULES = (
    {"id": "runtime", "label": "Runtime", "prefix": "nanobot/groupchat/runtime/", "role": "引擎 · 路由 · 工具循环"},
    {"id": "context", "label": "Context", "prefix": "nanobot/groupchat/context/", "role": "Prompt · 上下文"},
    {"id": "channels", "label": "Channels", "prefix": "nanobot/channels/", "role": "Telegram · 入站出站"},
    {"id": "groupchat", "label": "Groupchat", "prefix": "nanobot/groupchat/", "role": "群聊核心"},
    {"id": "providers", "label": "Providers", "prefix": "nanobot/providers/", "role": "LLM 提供商"},
    {"id": "cli", "label": "CLI", "prefix": "nanobot/cli/", "role": "命令行入口"},
    {"id": "agent", "label": "Agent", "prefix": "nanobot/agent/", "role": "兼容层"},
    {"id": "tests", "label": "Tests", "prefix": "tests/", "role": "测试"},
    {"id": "watch", "label": "Code-watch", "prefix": "scripts/code-watch/", "role": "本仪表盘"},
)

FLOW = {
    "nodes": [
        {"id": "channels", "label": "Channels", "detail": "Telegram / Discord / 飞书"},
        {"id": "manager", "label": "ChannelManager", "detail": "_route_inbound()"},
        {"id": "engine", "label": "GroupChatEngine", "detail": "inject()"},
        {"id": "direct", "label": "direct_chat", "detail": "1 agent · ~178 LOC"},
        {"id": "broadcast", "label": "broadcast_round", "detail": "2+ agents · ~4.3k LOC"},
        {"id": "outbound", "label": "MessageBus", "detail": "出站分发"},
    ],
    "edges": [
        ("channels", "manager"),
        ("manager", "engine"),
        ("engine", "direct"),
        ("engine", "broadcast"),
        ("direct", "outbound"),
        ("broadcast", "outbound"),
    ],
}


def nanobot_home() -> Path:
    return Path(os.environ.get("NANOBOT_HOME", Path.home() / ".nanobot"))


def _count_py_lines(root: Path, rel: str) -> int:
    base = root / rel
    if not base.exists():
        return 0
    total = 0
    for path in base.rglob("*.py"):
        if path.is_file():
            try:
                total += sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return total


def _module_for_path(path: str) -> str:
    for mod in MODULES:
        prefix = mod["prefix"]
        if prefix and path.startswith(prefix):
            return mod["id"]
    if path.startswith("nanobot/"):
        return "nanobot-other"
    return "other"


def module_stats(repo: Path) -> list[dict]:
    rows = []
    for mod in MODULES:
        lines = _count_py_lines(repo, mod["prefix"].rstrip("/")) if mod["prefix"] else 0
        rows.append({
            "id": mod["id"],
            "label": mod["label"],
            "role": mod["role"],
            "prefix": mod["prefix"],
            "lines": lines,
        })
    rows.append({
        "id": "nanobot-total",
        "label": "nanobot 合计",
        "role": "全部 Python",
        "prefix": "nanobot/",
        "lines": _count_py_lines(repo, "nanobot"),
    })
    return rows


def changes_by_module(files: list[dict]) -> list[dict]:
    counts: dict[str, int] = {m["id"]: 0 for m in MODULES}
    counts["nanobot-other"] = 0
    counts["other"] = 0
    for f in files:
        mid = _module_for_path(f["path"])
        counts[mid] = counts.get(mid, 0) + 1
    return [{"id": k, "count": v} for k, v in counts.items() if v]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_json(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _preview_text(path: Path, limit: int = _PREVIEW_CHARS) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _tail_lines(path: Path, n: int) -> list[str]:
    if not path.is_file() or n <= 0:
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except OSError:
        return []


def _gateway_status(home: Path) -> dict:
    pid_file = home / "logs" / "gateway.pid"
    log_file = home / "logs" / "gateway.log"
    pid = None
    detached = False

    try:
        from nanobot.headless import status as headless_status
        hs = headless_status()
        pid_file = hs.pid_file
        log_file = hs.log_file
        if hs.running and hs.pid:
            pid = hs.pid
            detached = True
    except Exception:
        pass

    if pid_file.is_file():
        try:
            file_pid = int(pid_file.read_text().strip())
            if file_pid and _pid_alive(file_pid):
                pid = file_pid
                detached = True
        except (OSError, ValueError):
            pass

    if not pid:
        try:
            import subprocess
            out = subprocess.run(
                ["pgrep", "-f", r"nanobot.*gateway"],
                capture_output=True,
                text=True,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                pid = int(out.stdout.strip().splitlines()[0])
        except (OSError, ValueError, IndexError):
            pid = None

    alive = bool(pid and _pid_alive(pid))
    return {
        "running": alive,
        "pid": pid if alive else None,
        "detached": detached and alive,
        "headless": alive,
        "mode": "background" if detached and alive else ("foreground" if alive else "stopped"),
        "pid_file": str(pid_file),
        "log_file": str(log_file),
        "log_tail": _tail_lines(log_file, _LOG_TAIL_LINES),
    }


def _prompt_manifest(home: Path) -> dict:
    data = _read_json(home / "prompt_manifest.json")
    return data if isinstance(data, dict) else {}


def _prompt_order(home: Path, manifest: dict) -> list[str]:
    order = _read_json(home / "prompt_order.json")
    if isinstance(order, list) and order:
        return [str(x) for x in order]
    components = manifest.get("components") if manifest else None
    if isinstance(components, dict):
        ranked = sorted(
            components.items(),
            key=lambda kv: (kv[1].get("order", 999), kv[0]),
        )
        return [k for k, _ in ranked]
    return [
        "main_prompt", "persona", "hard_rules", "tool_instructions", "skills",
        "user_context", "broadcast_hint", "group_context", "memory",
        "output_efficiency", "instructions", "leader_prompt",
        "history", "skills_overview", "examples", "group_nudge",
    ]


def prompt_stack(home: Path | None = None, *, mode: str = "direct") -> dict:
    home = home or nanobot_home()
    manifest = _prompt_manifest(home)
    components = manifest.get("components", {}) if manifest else {}
    order = _prompt_order(home, manifest)
    is_group = mode == "broadcast"

    stack = []
    for cid in order:
        meta = components.get(cid, {}) if isinstance(components, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        group_only = cid in _GROUP_ONLY_COMPONENTS
        active = is_group or not group_only
        source = meta.get("source_path")
        preview = ""
        if source:
            preview = _preview_text(home / str(source))
        stack.append({
            "id": cid,
            "label": meta.get("label", cid),
            "phase": meta.get("phase", "static"),
            "visibility": meta.get("visibility", "all"),
            "editable_by": meta.get("editable_by", "none"),
            "source_path": source,
            "group_only": group_only,
            "active": active,
            "preview": preview,
            "chars": len(preview) if preview else 0,
        })

    global_prompts = []
    prompts_dir = home / "prompts"
    if prompts_dir.is_dir():
        for path in sorted(prompts_dir.glob("*.md")):
            global_prompts.append({
                "name": path.name,
                "path": f"prompts/{path.name}",
                "chars": path.stat().st_size,
                "preview": _preview_text(path),
            })

    return {
        "order": order,
        "components": stack,
        "active_count": sum(1 for c in stack if c["active"]),
        "group_only_skipped": sum(1 for c in stack if c["group_only"] and not is_group),
        "global_prompts": global_prompts,
        "manifest_version": manifest.get("version"),
    }


def list_agents(home: Path | None = None, active: list[str] | None = None) -> list[dict]:
    home = home or nanobot_home()
    active_set = {a.lower() for a in (active or [])}
    agents_dir = home / "agents"
    rows: list[dict] = []

    if not agents_dir.is_dir():
        return rows

    for entry in sorted(agents_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        name = entry.name
        display = name[:1].upper() + name[1:] if name else name
        cfg = _read_json(entry / "config.json")
        model = ""
        rank = ""
        if isinstance(cfg, dict):
            model = str(cfg.get("model") or "")
            if not model:
                defaults = (cfg.get("agents") or {}).get("defaults") or {}
                if isinstance(defaults, dict):
                    model = str(defaults.get("model") or "")
            rank = str(cfg.get("rank") or "")

        ws = entry / "workspace"
        files = []
        for fname, fid, label in _AGENT_FILES:
            fpath = ws / fname
            if fpath.is_file():
                files.append({
                    "id": fid,
                    "label": label,
                    "filename": fname,
                    "path": str(fpath.relative_to(home)),
                    "chars": fpath.stat().st_size,
                    "preview": _preview_text(fpath),
                })

        rows.append({
            "id": name,
            "name": display,
            "active": display.lower() in active_set or name.lower() in active_set,
            "model": model,
            "rank": rank,
            "workspace": str(ws),
            "files": files,
            "soul_preview": next((f["preview"] for f in files if f["filename"] == "SOUL.md"), ""),
        })

    rows.sort(key=lambda r: (not r["active"], r["name"].lower()))
    return rows


def read_prompt_file(home: Path, rel_path: str) -> str | None:
    """Read a prompt file only if it lives under ~/.nanobot/prompts or agents/."""
    home = home.resolve()
    target = (home / rel_path).resolve()
    allowed = (home / "prompts", home / "agents")
    if not any(str(target).startswith(str(base.resolve())) for base in allowed):
        return None
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def web_channel_config(home: Path | None = None) -> dict:
    home = home or nanobot_home()
    cfg = _read_json(home / "config.json")
    if not isinstance(cfg, dict):
        return {"enabled": False, "host": "127.0.0.1", "port": 18791, "token": ""}
    web = cfg.get("channels", {}).get("web", {}) if isinstance(cfg.get("channels"), dict) else {}
    if not isinstance(web, dict):
        web = {}
    return {
        "enabled": bool(web.get("enabled", False)),
        "host": str(web.get("host", "127.0.0.1")),
        "port": int(web.get("port", 18791)),
        "token": str(web.get("token", "")),
    }


def chat_status(home: Path | None = None) -> dict:
    home = home or nanobot_home()
    web = web_channel_config(home)
    rt = runtime_snapshot(home)
    port_open = False
    if web["enabled"]:
        import socket
        try:
            with socket.create_connection((web["host"], web["port"]), timeout=0.4):
                port_open = True
        except OSError:
            port_open = False
    return {
        "web_channel": web,
        "gateway_running": rt["gateway"]["running"],
        "active_agents": rt["active_agents"],
        "mode": rt["mode"],
        "upstream_reachable": port_open,
        "ready": web["enabled"] and port_open and rt["gateway"]["running"],
        "ws_path": "/ws/chat",
    }


def recent_activity(home: Path | None = None, *, limit: int = _EVENT_TAIL_LINES) -> dict:
    home = home or nanobot_home()
    events_path = home / "logs" / "room_events.jsonl"
    events: list[dict] = []
    for line in _tail_lines(events_path, limit * 2):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                events.append(rec)
        except json.JSONDecodeError:
            continue
    events = events[-limit:]

    kinds: dict[str, int] = {}
    for ev in events:
        k = str(ev.get("kind", "unknown"))
        kinds[k] = kinds.get(k, 0) + 1

    return {
        "events": events,
        "event_count": len(events),
        "kinds": kinds,
        "log_path": str(events_path),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def runtime_snapshot(home: Path | None = None) -> dict:
    home = home or nanobot_home()
    agents: list[str] = []
    agents_path = home / "active_agents.json"
    if agents_path.is_file():
        try:
            data = json.loads(agents_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                agents = [str(a) for a in data]
        except (OSError, json.JSONDecodeError):
            pass

    n = len(agents)
    if n == 0:
        mode = "idle"
        route = None
        route_label = "无活跃 agent"
    elif n == 1:
        mode = "direct"
        route = "direct_chat"
        route_label = "1v1 Direct Chat"
    else:
        mode = "broadcast"
        route = "broadcast_round"
        route_label = f"群聊 Broadcast ({n} agents)"

    gw = _gateway_status(home)
    return {
        "nanobot_home": str(home),
        "active_agents": agents,
        "agent_count": n,
        "mode": mode,
        "route": route,
        "route_label": route_label,
        "gateway": gw,
        "headless": {
            "available": True,
            "running": gw["running"],
            "detached": gw.get("detached", False),
            "cli": "nanobot gateway  # 默认后台无头",
            "stop_cli": "nanobot gateway --stop",
            "foreground_cli": "nanobot gateway --foreground",
        },
    }


def agent_dashboard(home: Path | None = None) -> dict:
    home = home or nanobot_home()
    rt = runtime_snapshot(home)
    return {
        "runtime": rt,
        "agents": list_agents(home, rt.get("active_agents")),
        "prompt_stack": prompt_stack(home, mode=rt.get("mode", "idle")),
        "activity": recent_activity(home),
    }


def architecture(repo: Path, files: list[dict] | None = None) -> dict:
    files = files or []
    modules = module_stats(repo)
    changes = {c["id"]: c["count"] for c in changes_by_module(files)}
    for m in modules:
        m["changed"] = changes.get(m["id"], 0)
    home = nanobot_home()
    runtime = runtime_snapshot(home)
    active_route = runtime.get("route")
    dash = agent_dashboard(home)
    return {
        "flow": FLOW,
        "active_route": active_route,
        "modules": modules,
        "changes_by_module": changes_by_module(files),
        "runtime": runtime,
        "agents": dash["agents"],
        "prompt_stack": dash["prompt_stack"],
        "activity": dash["activity"],
        "shared": [
            "GroupChatEngine",
            "PromptBuilder",
            "HistoryContext",
            "tool_loop",
            "MessageBus",
        ],
    }