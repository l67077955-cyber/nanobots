---
name: cron
description: Schedule reminders and recurring tasks. Use when the user asks for timers, periodic checks, scheduled notifications, or one-time delayed tasks.
always: true
---

# Cron — Scheduled Tasks

Use the CLI script to manage scheduled tasks. The background service picks up changes automatically.

## CLI

```bash
python3 {baseDir}/scripts/cron_cli.py <command> [options]
```

## Add a Job

Recurring (every N seconds):
```bash
python3 {baseDir}/scripts/cron_cli.py add --message "Time to take a break!" --every 1200
```

Cron expression:
```bash
python3 {baseDir}/scripts/cron_cli.py add --message "Morning standup" --cron "0 9 * * 1-5" --tz "America/Vancouver"
```

One-time (compute ISO datetime from current time):
```bash
python3 {baseDir}/scripts/cron_cli.py add --message "Remind me about the meeting" --at "<ISO datetime>"
```

## List / Remove

```bash
python3 {baseDir}/scripts/cron_cli.py list
python3 {baseDir}/scripts/cron_cli.py remove --id abc123
```

## Time Expressions

| User says | CLI flags |
|-----------|-----------|
| every 20 minutes | `--every 1200` |
| every hour | `--every 3600` |
| every day at 8am | `--cron "0 8 * * *"` |
| weekdays at 5pm | `--cron "0 17 * * 1-5"` |
| 9am Vancouver time daily | `--cron "0 9 * * *" --tz "America/Vancouver"` |
| at a specific time | `--at <ISO datetime>` (compute from current time) |

## Timezone

Use `--tz` with `--cron` to schedule in a specific IANA timezone. Without `--tz`, the server's local timezone is used.

## Important

- When the user says "remind me every N minutes/hours" → use `--every`
- When the user says "remind me at 9am every day" → use `--cron`
- When the user says "remind me in 30 minutes" → compute the ISO datetime and use `--at`
- Do NOT simulate timers yourself — always use this CLI script.
