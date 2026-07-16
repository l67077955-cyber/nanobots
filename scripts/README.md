# scripts/

Dev and ops helpers for this repo. **Not** the same as `nanobot/tools/` (LLM tools).

| Path | Role |
|------|------|
| `code-watch/` | Live dashboard / agent insights |
| `core_agent_lines.sh` | Line counts for core packages |
| `headless_*.py`, `live_chat.py` | Local gateway / chat smoke |
| `complex_task_monitor.py` | Broadcast / complex-task checks |
| `migrate_to_manifest.py`, `regen_cb_agents.py` | One-off migrations |
| `capture-config.sh`, `switch-with-config.sh`, `switch-debug-copy.sh` | Pair source checkout with `~/.nanobot` snapshots |
| `tag-cleanup.sh` | Tag maintenance |
| `bench_matrix.yaml` | Bench matrix |

Runtime data lives under `~/.nanobot`, not here.
