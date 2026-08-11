#!/usr/bin/env python3
"""CLI for managing nanobot providers, models, and settings.

Usage (from exec tool):
  python3 {baseDir}/scripts/settings_cli.py providers list
  python3 {baseDir}/scripts/settings_cli.py providers add --name aihub --url https://api.example.com/v1 --key sk-xxx
  python3 {baseDir}/scripts/settings_cli.py providers edit --name aihub --url https://new-url.com/v1
  python3 {baseDir}/scripts/settings_cli.py providers edit --name aihub --key sk-newkey
  python3 {baseDir}/scripts/settings_cli.py providers remove --name aihub
  python3 {baseDir}/scripts/settings_cli.py models list
  python3 {baseDir}/scripts/settings_cli.py models add --provider aihub --model anthropic/claude-sonnet-4-5
  python3 {baseDir}/scripts/settings_cli.py models remove --provider aihub --model anthropic/claude-sonnet-4-5
"""

import argparse
import sys


def _load() -> dict:
    from nanobot.state.settings_store import load_pm as _store_load_pm
    return _store_load_pm()


def _save(data: dict) -> None:
    from nanobot.state.settings_store import save_pm as _store_save_pm
    _store_save_pm(data)


# ── Providers ──

def providers_list(args):
    pm = _load()
    provs = pm.get("providers", {})
    models = pm.get("models", {})
    if not provs:
        print("No providers configured.")
        return
    for name, info in provs.items():
        url = info.get("url", "?")
        key = info.get("apiKey", "")
        key_preview = key[:8] + "..." if key else "(none)"
        ms = models.get(name, [])
        print(f"- {name}")
        print(f"  url: {url}")
        print(f"  key: {key_preview}")
        if ms:
            print(f"  models: {', '.join(ms)}")
        else:
            print("  models: (none)")


def providers_add(args):
    if not args.name or not args.url or not args.key:
        print("Error: --name, --url, --key required", file=sys.stderr)
        sys.exit(1)
    pm = _load()
    if args.name in pm.get("providers", {}):
        print(f"Error: provider '{args.name}' already exists, use 'edit'", file=sys.stderr)
        sys.exit(1)
    pm.setdefault("providers", {})[args.name] = {"url": args.url, "apiKey": args.key}
    pm.setdefault("models", {})[args.name] = []
    _save(pm)
    print(f"Added provider '{args.name}' ({args.url})")


def providers_edit(args):
    if not args.name:
        print("Error: --name required", file=sys.stderr)
        sys.exit(1)
    pm = _load()
    if args.name not in pm.get("providers", {}):
        print(f"Error: provider '{args.name}' not found", file=sys.stderr)
        sys.exit(1)
    info = pm["providers"][args.name]
    changed = []
    if args.url:
        info["url"] = args.url
        changed.append("url")
    if args.key:
        info["apiKey"] = args.key
        changed.append("key")
    if not changed:
        print("Nothing to change (specify --url or --key)")
        return
    _save(pm)
    print(f"Updated provider '{args.name}': {', '.join(changed)}")


def providers_remove(args):
    if not args.name:
        print("Error: --name required", file=sys.stderr)
        sys.exit(1)
    pm = _load()
    if args.name not in pm.get("providers", {}):
        print(f"Error: provider '{args.name}' not found", file=sys.stderr)
        sys.exit(1)
    del pm["providers"][args.name]
    pm.get("models", {}).pop(args.name, None)
    _save(pm)
    print(f"Removed provider '{args.name}'")


# ── Models ──

def models_list(args):
    pm = _load()
    models = pm.get("models", {})
    if not models:
        print("No models configured.")
        return
    for prov, ms in models.items():
        if ms:
            for m in ms:
                print(f"- {m}  ({prov})")


def models_add(args):
    if not args.provider or not args.model:
        print("Error: --provider, --model required", file=sys.stderr)
        sys.exit(1)
    pm = _load()
    if args.provider not in pm.get("providers", {}):
        print(f"Error: provider '{args.provider}' not found", file=sys.stderr)
        sys.exit(1)
    ms = pm.setdefault("models", {}).setdefault(args.provider, [])
    if args.model in ms:
        print(f"Model '{args.model}' already exists under '{args.provider}'")
        return
    ms.append(args.model)
    _save(pm)
    print(f"Added model '{args.model}' to '{args.provider}'")


def models_remove(args):
    if not args.provider or not args.model:
        print("Error: --provider, --model required", file=sys.stderr)
        sys.exit(1)
    pm = _load()
    ms = pm.get("models", {}).get(args.provider, [])
    if args.model not in ms:
        print(f"Error: model '{args.model}' not found under '{args.provider}'", file=sys.stderr)
        sys.exit(1)
    ms.remove(args.model)
    _save(pm)
    print(f"Removed model '{args.model}' from '{args.provider}'")


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="nanobot settings CLI")
    sub = parser.add_subparsers(dest="resource", required=True)

    # providers
    prov = sub.add_parser("providers", help="Manage providers")
    prov_sub = prov.add_subparsers(dest="action", required=True)

    prov_sub.add_parser("list", help="List providers")

    add_p = prov_sub.add_parser("add", help="Add provider")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--url", required=True, help="API base URL")
    add_p.add_argument("--key", required=True, help="API key")

    edit_p = prov_sub.add_parser("edit", help="Edit provider")
    edit_p.add_argument("--name", required=True)
    edit_p.add_argument("--url", help="New URL")
    edit_p.add_argument("--key", help="New API key")

    rm_p = prov_sub.add_parser("remove", help="Remove provider")
    rm_p.add_argument("--name", required=True)

    # models
    mod = sub.add_parser("models", help="Manage models")
    mod_sub = mod.add_subparsers(dest="action", required=True)

    mod_sub.add_parser("list", help="List models")

    madd = mod_sub.add_parser("add", help="Add model")
    madd.add_argument("--provider", required=True)
    madd.add_argument("--model", required=True, help="Model ID (e.g. anthropic/claude-sonnet-4-5)")

    mrm = mod_sub.add_parser("remove", help="Remove model")
    mrm.add_argument("--provider", required=True)
    mrm.add_argument("--model", required=True)

    args = parser.parse_args()

    dispatch = {
        ("providers", "list"): providers_list,
        ("providers", "add"): providers_add,
        ("providers", "edit"): providers_edit,
        ("providers", "remove"): providers_remove,
        ("models", "list"): models_list,
        ("models", "add"): models_add,
        ("models", "remove"): models_remove,
    }
    fn = dispatch.get((args.resource, args.action))
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
