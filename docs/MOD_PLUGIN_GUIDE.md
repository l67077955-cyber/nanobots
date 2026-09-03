# Mod Plugin Guide

Mods are the **only sanctioned way** to add behavioural features to nanobot's
group-chat orchestration. If you are an agent (or a human) about to edit
`broadcast.py` / `mailbox.py` / `tool_loop.py` to add a guard, a reminder, a
metric, or a policy — stop. Write a mod instead. Core files are for fixing
core bugs, and every core edit needs a regression test first.

## The mental model

```
orchestration core ──emit──▶ event bus ──▶ mods (isolated, config-gated)
        ▲                                     │
        └──── tier-2 mutable payload ──────────┘   (e.g. `inject` lists)
```

- Core code emits typed events at a small set of chokepoints
  (`nanobot/groupchat/orchestra/events.py` has the catalogue).
- Mods subscribe, observe, and — for filter events — append to mutable
  payload containers the core applies afterwards.
- A failing mod is logged and contained. It can never break a round.

## Authoring a mod (workspace layer — no repo access needed)

Create `~/.nanobot/mods/<name>/mod.py`:

```python
from nanobot.mods.base import Mod

class MoodStatMod(Mod):
    name = "moodstat"
    description = "统计用户消息量并汇报"

    def default_config(self):
        return {"report_every": 10}

    async def start(self, ctx):
        self._count = 0
        self._every = int(ctx.config.get("report_every", 10))
        self._send = ctx.send

    async def on_user_message_delivered(self, *, message, **kw):
        self._count += 1
        if self._count % self._every == 0:
            await self._send(f"📊 已收到 {self._count} 条用户消息")
```

Rules:

1. **Handler naming**: event `user:message_delivered` → method
   `on_user_message_delivered` (`:` → `_`). Always accept `**kw` — new
   payload fields must not break you.
2. **Tier 1 (observe)**: read payloads, use `ctx.send` for displays, write
   your own files. Do not import engine/mailbox internals.
3. **Tier 2 (filter)**: payloads may carry mutable containers (`inject` on
   `agent:reactivated` is the current example). Append; never replace.
4. No monkey-patching, no imports of `nanobot.groupchat.orchestra.broadcast`
   — only `nanobot.mods.base` and, for extra subscriptions, the `ctx.bus`.

## Enabling

`~/.nanobot/mods.json`:

```json
{
  "moodstat": { "enabled": true, "report_every": 5 },
  "round_telemetry": { "enabled": true }
}
```

Restart the gateway (or let a code-watch session pick it up) — the mod list
is read at startup. Builtin mods shadow same-named workspace/external mods.

## Event catalogue (stable surface)

See `EVENTS` in `nanobot/groupchat/orchestra/events.py`. Highlights:

| Event | Fires when | Payload notes |
|---|---|---|
| `user:round_opened` | message opens a new round | `user_input`, `agent_count` |
| `user:message_delivered` / `user:message_requeued` | mid-round interjection / wind-down parking | `message`, `delivered_to`, `interrupted` |
| `round:started` / `round:winding_down` / `round:reopened` / `round:ended` | lifecycle transitions | `reason` on winding_down |
| `agent:interrupted` / `agent:waiting` / `agent:done` | per-agent state | `by` on interrupted |
| `agent:reactivated` | agent woken from auto-wait | **tier-2**: `inject` list, `recent_texts` |
| `message:delivered` | agent→agent mailbox send | `sender`, `targets`, `delivered` |
| `tool:result` | every tool result | `tool`, `ok`, `chars` |

Adding an event = add it to `EVENTS`, emit from one chokepoint, document
payload fields here. Removing/renaming = breaking change, don't.

## Shipping a builtin mod

Put it under `nanobot/mods/builtin/<name>.py`, add tests in
`tests/test_mods.py` (or a sibling file), and register defaults in
`nanobot/mods/manager.py::_DEFAULTS` **only** if it replaces migrated inline
behaviour (that's what keeps out-of-the-box behaviour identical).

## Current builtins

- **antirepeat** (on by default): migrated from inline broadcast code — the
  anti-repeat reminder injected on teammate wake-ups.
- **round_telemetry** (off): structured JSONL telemetry of round/agent/user/
  tool events to `~/.nanobot/mods-telemetry/telemetry.jsonl`.

## Not yet available (tier 3)

Tool contribution (registering new agent tools via mods) is deliberately
out of scope for v1 — use MCP servers (`tools.mcpServers` in config.json)
for external capabilities today.
