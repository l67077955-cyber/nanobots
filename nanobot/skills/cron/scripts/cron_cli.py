#!/usr/bin/env python3
"""CLI wrapper for nanobot cron service — manages jobs via jobs.json.

Usage (from exec tool):
  python {baseDir}/scripts/cron_cli.py add --message "Check stars" --every 600
  python {baseDir}/scripts/cron_cli.py add --message "Standup" --cron "0 9 * * 1-5" --tz "America/Vancouver"
  python {baseDir}/scripts/cron_cli.py add --message "Meeting" --at "2026-03-25T10:00:00"
  python {baseDir}/scripts/cron_cli.py list
  python {baseDir}/scripts/cron_cli.py remove --id abc123

The CronService background timer detects file changes via mtime and picks up
new/removed jobs automatically.
"""

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path


def _store_path() -> Path:
    """Resolve the cron jobs.json path, matching CronService conventions."""
    # Check NANOBOT_CRON_DIR first, fall back to ~/.nanobot/cron
    import os
    cron_dir = os.environ.get("NANOBOT_CRON_DIR")
    if cron_dir:
        p = Path(cron_dir)
    else:
        p = Path.home() / ".nanobot" / "cron"
    p.mkdir(parents=True, exist_ok=True)
    return p / "jobs.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_store(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "jobs": []}


def _save_store(path: Path, store: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


def _compute_next_run(schedule: dict, now_ms: int) -> int | None:
    kind = schedule["kind"]
    if kind == "at":
        at_ms = schedule.get("atMs")
        return at_ms if at_ms and at_ms > now_ms else None
    if kind == "every":
        every_ms = schedule.get("everyMs")
        return now_ms + every_ms if every_ms and every_ms > 0 else None
    if kind == "cron":
        expr = schedule.get("expr")
        if not expr:
            return None
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter
            tz_str = schedule.get("tz")
            tz = ZoneInfo(tz_str) if tz_str else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(now_ms / 1000, tz=tz)
            cron = croniter(expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception as e:
            print(f"Warning: could not compute next cron run: {e}", file=sys.stderr)
            return None
    return None


def _format_timing(schedule: dict) -> str:
    kind = schedule["kind"]
    if kind == "cron":
        tz = f" ({schedule['tz']})" if schedule.get("tz") else ""
        return f"cron: {schedule.get('expr', '?')}{tz}"
    if kind == "every":
        ms = schedule.get("everyMs", 0)
        if ms % 3_600_000 == 0:
            return f"every {ms // 3_600_000}h"
        if ms % 60_000 == 0:
            return f"every {ms // 60_000}m"
        if ms % 1000 == 0:
            return f"every {ms // 1000}s"
        return f"every {ms}ms"
    if kind == "at":
        at_ms = schedule.get("atMs")
        if at_ms:
            dt = datetime.fromtimestamp(at_ms / 1000).astimezone()
            return f"at {dt.isoformat()}"
    return kind


# ── Commands ──────────────────────────────────────────────────

def cmd_add(args, store_path: Path) -> None:
    if not args.message:
        print("Error: --message is required", file=sys.stderr)
        sys.exit(1)

    # Build schedule
    now = _now_ms()
    delete_after = False

    if args.every:
        schedule = {"kind": "every", "everyMs": args.every * 1000}
    elif args.cron:
        if args.tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(args.tz)
            except (KeyError, Exception):
                print(f"Error: unknown timezone '{args.tz}'", file=sys.stderr)
                sys.exit(1)
        schedule = {"kind": "cron", "expr": args.cron, "tz": args.tz}
    elif args.at:
        try:
            dt = datetime.fromisoformat(args.at)
        except ValueError:
            print(f"Error: invalid ISO datetime '{args.at}'", file=sys.stderr)
            sys.exit(1)
        at_ms = int(dt.timestamp() * 1000)
        schedule = {"kind": "at", "atMs": at_ms}
        delete_after = True
    else:
        print("Error: one of --every, --cron, or --at is required", file=sys.stderr)
        sys.exit(1)

    # Determine delivery target from environment (set by _set_tool_context)
    import os
    channel = os.environ.get("NANOBOT_CHANNEL", "cli")
    chat_id = os.environ.get("NANOBOT_CHAT_ID", "direct")

    job = {
        "id": str(uuid.uuid4())[:8],
        "name": args.message[:30],
        "enabled": True,
        "schedule": schedule,
        "payload": {
            "kind": "agent_turn",
            "message": args.message,
            "deliver": True,
            "channel": channel,
            "to": chat_id,
        },
        "state": {
            "nextRunAtMs": _compute_next_run(schedule, now),
            "lastRunAtMs": None,
            "lastStatus": None,
            "lastError": None,
            "runHistory": [],
        },
        "createdAtMs": now,
        "updatedAtMs": now,
        "deleteAfterRun": delete_after,
    }

    store = _load_store(store_path)
    store["jobs"].append(job)
    _save_store(store_path, store)
    print(f"Created job '{job['name']}' (id: {job['id']})")


def cmd_list(args, store_path: Path) -> None:
    store = _load_store(store_path)
    jobs = [j for j in store.get("jobs", []) if j.get("enabled", True)]
    if not jobs:
        print("No scheduled jobs.")
        return
    for j in sorted(jobs, key=lambda x: (x.get("state", {}).get("nextRunAtMs") or float("inf"))):
        timing = _format_timing(j["schedule"])
        msg = j.get("payload", {}).get("message", j.get("name", "?"))
        line = f"- {j['name']} (id: {j['id']}, {timing})"
        state = j.get("state", {})
        if state.get("lastRunAtMs"):
            last_dt = datetime.fromtimestamp(state["lastRunAtMs"] / 1000).astimezone()
            status = state.get("lastStatus", "unknown")
            line += f"\n  Last run: {last_dt.isoformat()} — {status}"
        if state.get("nextRunAtMs"):
            next_dt = datetime.fromtimestamp(state["nextRunAtMs"] / 1000).astimezone()
            line += f"\n  Next run: {next_dt.isoformat()}"
        print(line)


def cmd_remove(args, store_path: Path) -> None:
    if not args.id:
        print("Error: --id is required", file=sys.stderr)
        sys.exit(1)
    store = _load_store(store_path)
    before = len(store["jobs"])
    store["jobs"] = [j for j in store["jobs"] if j["id"] != args.id]
    if len(store["jobs"]) < before:
        _save_store(store_path, store)
        print(f"Removed job {args.id}")
    else:
        print(f"Job {args.id} not found", file=sys.stderr)
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="nanobot cron CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="Add a scheduled job")
    add_p.add_argument("--message", "-m", required=True, help="Task/reminder message")
    add_p.add_argument("--every", type=int, help="Interval in seconds")
    add_p.add_argument("--cron", help="Cron expression (e.g. '0 9 * * *')")
    add_p.add_argument("--tz", help="IANA timezone (e.g. 'America/Vancouver')")
    add_p.add_argument("--at", help="ISO datetime for one-time execution")

    sub.add_parser("list", help="List scheduled jobs")

    rm_p = sub.add_parser("remove", help="Remove a job")
    rm_p.add_argument("--id", required=True, help="Job ID to remove")

    args = parser.parse_args()
    sp = _store_path()

    if args.command == "add":
        cmd_add(args, sp)
    elif args.command == "list":
        cmd_list(args, sp)
    elif args.command == "remove":
        cmd_remove(args, sp)


if __name__ == "__main__":
    main()
