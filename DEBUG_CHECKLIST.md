# nanobot 服务器调试清单

按频率排查 gateway。命令与路径来自 `nanobot/cli/commands.py`、`nanobot/headless.py`、`~/.nanobot/restart_nanobot.sh`、`nanobot/skills/debug/SKILL.md`。

---

## 1. 运行状态

```bash
nanobot status
pgrep -f "nanobot gateway" 2>/dev/null || true
cat ~/.nanobot/logs/gateway.pid
ps aux | grep nanobot | grep -v grep
```

PID：`~/.nanobot/logs/gateway.pid`（`headless.py:31`）。日志：`~/.nanobot/logs/gateway.log`（`headless.py:35`）。

---

## 2. 日志

```bash
tail -30 ~/.nanobot/logs/gateway.log
grep -i 'error\|traceback\|failed' ~/.nanobot/logs/gateway.log | tail -20
timeout 10 tail -f ~/.nanobot/logs/gateway.log
grep -i cron ~/.nanobot/logs/gateway.log | tail -10
grep -i heartbeat ~/.nanobot/logs/gateway.log | tail -10
```

会话日志（`commands.py`）：

```bash
nanobot logs --session gc-20260620-141846
nanobot logs timeline --session gc-20260620-141846 --last 30
nanobot logs grep error --session gc-20260620-141846 --limit 20
```

`restart_nanobot.sh` 另写 `~/.nanobot/gateway.log`（`restart_nanobot.sh:15`），与 `logs/gateway.log` 并存时需两处都查。

---

## 3. 配置

```bash
cat ~/.nanobot/config.yaml 2>/dev/null || cat ~/.nanobot/config.json 2>/dev/null | python3 -m json.tool
ls -la ~/.nanobot/workspace/
du -sh ~/.nanobot/*
```

```bash
nanobot gateway --config ~/.nanobot/config.json --workspace ~/.nanobot/workspace
```

---

## 4. 通道与 Provider

```bash
nanobot channels status
nanobot plugins list
nanobot status
nanobot provider login openai-codex
nanobot provider login github-copilot
nanobot channels login
```

---

## 5. Cron、Heartbeat

```bash
cat ~/.nanobot/cron/jobs.json | python3 -m json.tool
```

Cron / Heartbeat 是否在跑：见 §2 的 `grep -i cron` / `grep -i heartbeat`。

---

## 6. 重启与恢复

```bash
nanobot gateway --stop
nanobot gateway
nanobot gateway --foreground --verbose
nanobot gateway --port 18790
```

```bash
~/.nanobot/restart_nanobot.sh
```

`restart_nanobot.sh` 内部（`restart_nanobot.sh:5-17`）：

```bash
pgrep -f "nanobot gateway" 2>/dev/null || true
cd /root/.nanobot
nohup bash -lic 'cd /root/.nanobot && nanobot gateway' > /root/.nanobot/gateway.log 2>&1 &
tail -3 /root/.nanobot/gateway.log 2>/dev/null
```

恢复顺序：`nanobot status` → `tail -30 ~/.nanobot/logs/gateway.log` → `nanobot gateway --stop` → `nanobot gateway --foreground --verbose` → `Ctrl+C` → `nanobot gateway`。

---

## 7. 路径速查

- 配置：`~/.nanobot/config.json`
- Gateway 日志：`~/.nanobot/logs/gateway.log`（`headless.py:35`）
- Gateway PID：`~/.nanobot/logs/gateway.pid`（`headless.py:31`）
- restart 脚本日志：`~/.nanobot/gateway.log`（`restart_nanobot.sh:15`）
- Workspace：`~/.nanobot/workspace/`
- Cron：`~/.nanobot/cron/jobs.json`
- 重启脚本：`~/.nanobot/restart_nanobot.sh`