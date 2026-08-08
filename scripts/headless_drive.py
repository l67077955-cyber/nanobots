"""Headless nanobot CLI: run the group chat, or administer it — no Telegram.

Two modes:

  RUN (default) — send a message and let the standard group-chat round run,
    exactly the live gateway path (engine.inject → broadcast round), same as
    /new or messaging the bot.

      python scripts/headless_drive.py "消息"          # one-shot round
      python scripts/headless_drive.py                 # REPL: type a line → a round

  ADMIN — add/edit/delete agents, providers, models, groups via a thin CLI over
    the shared settings store (nanobot/state/settings_store.py) + engine, so
    storage changes have a single write-site:

      python scripts/headless_drive.py admin agents list
      python scripts/headless_drive.py admin agents add  Kirk
      python scripts/headless_drive.py admin agents remove Harper
      python scripts/headless_drive.py admin agents rm    Scholar            # delete on disk
      python scripts/headless_drive.py admin agents create MyAgent model:... "人设可选"
      python scripts/headless_drive.py admin agents edit  MyAgent model anthropic/claude-sonnet-4-5
      python scripts/headless_drive.py admin agents edit  MyAgent prompt 新的人设
      python scripts/headless_drive.py admin providers                      # list
      python scripts/headless_drive.py admin providers add aihub https://api.x.com/v1 sk-xxx
      python scripts/headless_drive.py admin providers edit aihub http://new/v1 -   # 改 URL,key 不变
      python scripts/headless_drive.py admin providers rm  aihub
      python scripts/headless_drive.py admin models
      python scripts/headless_drive.py admin models add aihub anthropic/claude-sonnet-4-5
      python scripts/headless_drive.py admin models rm  aihub anthropic/claude-sonnet-4-5
      python scripts/headless_drive.py admin groups
      python scripts/headless_drive.py admin groups save 研报组
      python scripts/headless_drive.py admin groups load 研报组
      python scripts/headless_drive.py admin groups rm   研报组

Safe alongside the systemd gateway: no Telegram polling → no bot conflict,
no single-instance lock.
"""
import argparse
import asyncio
import logging
import sys
import time

logging.disable(logging.WARNING)
from loguru import logger as _lg
_lg.remove()
_lg.add(sys.stderr, level="WARNING", colorize=False)

from nanobot.state import settings_store as store  # noqa: E402

CYAN = "\033[36m"; GREEN = "\033[32m"; YELLOW = "\033[33m"; RESET = "\033[0m"


# ── RUN mode ────────────────────────────────────────────────────────────────

def frame(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] ── 广播 ──\n{text}\n{'─' * 44}", flush=True)


async def wait_round(engine, quiet: float, max_wait: float) -> bool:
    baseline = len(engine._outbox)
    last, start = time.time(), time.time()
    while time.time() - start < max_wait:
        if len(engine._outbox) != baseline:
            baseline, last = len(engine._outbox), time.time()
        elif time.time() - last >= quiet:
            return True
        await asyncio.sleep(0.5)
    return False


async def run_round(engine, msg: str, quiet: float, max_wait: float) -> None:
    engine.inject(msg)
    print(f"⟶ {msg}", flush=True)
    done = await wait_round(engine, quiet, max_wait)
    print(f"⟵ 本轮{'完成' if done else '超时未收敛'}· agents={engine.active_agents}", flush=True)


def build_engine():
    from nanobot.config.loader import load_config
    from nanobot.cli.commands import _make_provider
    from nanobot.groupchat.orchestra.engine import GroupChatEngine

    cfg = load_config(None)
    provider = _make_provider(cfg)
    engine = GroupChatEngine(
        config=cfg.groupchat, provider=provider, workspace=cfg.workspace_path,
        web_search_config=cfg.tools.web.search, web_proxy=cfg.tools.web.proxy or None,
    )
    return engine


async def cmd_run(args) -> None:
    engine = build_engine()
    engine._outbox = []
    async def send_fn(text: str) -> None:
        engine._outbox.append(text)
        frame(text)
    engine.set_send_fn(send_fn)
    print(f"活跃 agents: {engine.active_agents}", flush=True)
    try:
        if args.text:
            await run_round(engine, args.text, args.quiet, args.wait)
        else:
            while True:
                try:
                    line = (await asyncio.get_running_loop().run_in_executor(
                        None, lambda: input("\n>>> "))).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    break
                await run_round(engine, line, args.quiet, args.wait)
    finally:
        if engine._running:
            engine.stop()


# ── ADMIN mode ──────────────────────────────────────────────────────────────

def _p(name):  # key preview, hide secrets
    return name[:8] + "…" if name else "(未设置)"


def admin_agents(argv: list[str]) -> None:
    if not argv or argv[0] == "list":
        eng = build_engine()
        active = eng.active_agents
        print(f"{YELLOW}活跃{RESET}: {', '.join(active) or '(空)'}")
        print(f"{YELLOW}注册表{RESET}: {', '.join(eng.registry.keys())}")
        return
    op, name = argv[0], argv[1]
    eng = build_engine()
    if op == "add":
        print(eng.add_agent(name))
    elif op in ("remove", "rm", "del"):
        if op == "rm" or op == "del":
            if not eng.delete_agent(name):
                print(f"⚠️ {name} 不存在或已删除")
                return
            print(f"🗑 已删除 agent {name} (磁盘 config 已删除)")
        else:
            print(eng.remove_agent(name))
    elif op == "create":
        model = argv[2] if len(argv) > 2 else None
        prompt = argv[3] if len(argv) > 3 else None
        if not model:
            print("用法: admin agents create <name> <model> [prompt]"); return
        store.create_agent(name, model, prompt)
        print(f"✅ 已创建 {name} (model={model})")
    elif op == "edit":
        field = argv[2] if len(argv) > 2 else None
        value = argv[3] if len(argv) > 3 else None
        if not field or value is None:
            print(f"用法: admin agents edit <name> <field> <value>  (fields: {sorted(store.EDITABLE_AGENT_FIELDS)})")
            return
        try:
            store.update_agent(name, field, value)
            print(f"✅ {name}.{field} = {value}")
        except (KeyError, ValueError) as e:
            print(f"❌ {e}")
    else:
        print(f"未知 agent 操作: {op}")


def admin_providers(argv: list[str]) -> None:
    if not argv or argv[0] == "list":
        pm = store.load_pm()
        for n, info in pm["providers"].items():
            ms = pm["models"].get(n, [])
            print(f"- {n}\n  url: {info.get('url')}\n  key: {_p(info.get('apiKey'))}\n  models: {', '.join(ms) or '(无)'}")
        if not pm["providers"]:
            print("(无 provider)")
        return
    op, name = argv[0], argv[1]
    try:
        if op == "add":
            url = argv[2]; key = argv[3]
            store.add_provider(name, url, key)
            print(f"✅ 已添加 provider {name}")
        elif op == "edit":
            url = argv[2] if len(argv) > 2 and argv[2] != "-" else None
            key = argv[3] if len(argv) > 3 and argv[3] != "-" else None
            if not url and not key:
                print("用法: admin providers edit <name> [<new-url>|-] [<new-key>|-]"); return
            store.update_provider(name, url=url, api_key=key)
            print(f"✅ 已更新 provider {name}")
        elif op in ("rm", "del"):
            store.delete_provider(name)
            print(f"🗑 已删除 provider {name}")
        else:
            print(f"未知 provider 操作: {op}")
    except (KeyError, ValueError, IndexError) as e:
        print(f"❌ {e}")


def admin_models(argv: list[str]) -> None:
    if not argv or argv[0] == "list":
        for prov, ms in store.list_models().items():
            print(f"{prov}: {', '.join(ms) or '(无)'}")
        return
    op = argv[0]
    try:
        if op == "add":
            store.add_model(argv[1], argv[2]); print(f"✅ {argv[1]} += {argv[2]}")
        elif op in ("rm", "del"):
            store.delete_model(argv[1], argv[2]); print(f"🗑 {argv[1]} -= {argv[2]}")
        else:
            print(f"未知 model 操作: {op}")
    except (KeyError, ValueError, IndexError) as e:
        print(f"❌ {e}")


def admin_groups(argv: list[str]) -> None:
    eng = build_engine()
    if not argv or argv[0] == "list":
        groups = eng.load_groups()
        for g, members in groups.items():
            print(f"- {g}: {', '.join(members)}")
        if not groups:
            print("(无分组, 用 admin groups save <名> 保存当前活跃成员)")
        return
    op, name = argv[0], argv[1]
    if op == "save":
        print(eng.save_group(name))
    elif op == "load":
        groups = eng.load_groups()
        if name not in groups:
            print(f"❌ 分组 '{name}' 不存在"); return
        want = groups[name]
        for n in list(eng.active_agents):
            if n not in want:
                eng.remove_agent(n)
        for n in want:
            if n not in eng.active_agents:
                r = eng.add_agent(n)
                print(r)
        print(f"👥 已载入分组 {name}: {', '.join(eng.active_agents)}")
    elif op in ("rm", "del"):
        print(eng.delete_group(name))
    else:
        print(f"未知 group 操作: {op}")


def cmd_admin(argv: list[str]) -> None:
    if not argv:
        print(__doc__.split("ADMIN")[0].split("Two modes")[1]); return
    res = argv[0]
    rest = argv[1:]
    if res == "agents":
        admin_agents(rest)
    elif res == "providers":
        admin_providers(rest)
    elif res == "models":
        admin_models(rest)
    elif res == "groups":
        admin_groups(rest)
    else:
        print(f"未知资源: {res} (可选 agents/providers/models/groups)")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "admin":
        # Admin mode: pass arguments straight through (raw) so resource-level
        # flags/values never collide with run-mode argparse options.
        cmd_admin(argv[1:])
        return

    ap = argparse.ArgumentParser(description="Headless nanobot CLI: run the group chat")
    ap.add_argument("text", nargs="*", default=[], help="the message to run a round on")
    ap.add_argument("--quiet", type=float, default=20.0)
    ap.add_argument("--wait", type=float, default=180.0)
    args = ap.parse_args(argv)

    run_args = argparse.Namespace(
        text=" ".join(args.text) or None, quiet=args.quiet, wait=args.wait,
    )
    asyncio.run(cmd_run(run_args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断")