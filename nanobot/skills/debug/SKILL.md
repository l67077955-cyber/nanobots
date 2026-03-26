---
name: debug
description: "Diagnose system issues: check logs, process status, cron jobs, connectivity. Use when something isn't working or user asks to debug."
always: true
---

# Debug — 系统诊断 & 实时推送

当出现问题或用户要求排查时使用。通过 exec 运行诊断命令。

## 发送消息到 Telegram

用 `send_cli.py` 从脚本内部直接推送消息到 Telegram：

```bash
python3 {baseDir}/scripts/send_cli.py --chat-id <CHAT_ID> --text "消息内容"
```

> **CHAT_ID**: 从当前对话的 context 获取。环境变量 `NANOBOT_CHAT_ID` 在 exec 中可用。

### 实时输出脚本模式

当用户要求脚本结果实时投递到 Telegram 时，写脚本调用 send_cli：

```python
#!/usr/bin/env python3
import subprocess, time, os

SEND = "{baseDir}/scripts/send_cli.py"
CHAT_ID = os.environ.get("NANOBOT_CHAT_ID", "<填入chat_id>")

def send(text):
    subprocess.run(["python3", SEND, "--chat-id", CHAT_ID, "--text", text])

for i in range(6):
    send(f"⏱ {time.strftime('%H:%M:%S')} — tick {i+1}")
    time.sleep(20)

send("✅ 完成")
```

## 查看日志

```bash
# 最近 nanobot 日志
exec command="tail -30 /tmp/nanobot.log"

# 搜索错误
exec command="grep -i 'error\|traceback\|failed' /tmp/nanobot.log | tail -20"

# 实时跟踪 (配合 timeout 避免卡住)
exec command="timeout 10 tail -f /tmp/nanobot.log"
```

## 进程状态

```bash
# nanobot 进程
exec command="ps aux | grep nanobot | grep -v grep"

# 系统资源
exec command="free -h && echo '---' && df -h / && echo '---' && uptime"

# Python 进程
exec command="ps aux | grep python | grep -v grep"
```

## Cron 诊断

```bash
# 当前 cron 任务
exec command="python3 {baseDir}/../cron/scripts/cron_cli.py list"

# 查看 jobs.json 原始数据
exec command="cat ~/.nanobot/cron/jobs.json | python3 -m json.tool"

# 检查 cron 服务是否在运行 (看日志)
exec command="grep -i cron /tmp/nanobot.log | tail -10"
```

## 配置检查

```bash
# 提供商配置
exec command="python3 {baseDir}/../settings/scripts/settings_cli.py providers list"

# 模型列表
exec command="python3 {baseDir}/../settings/scripts/settings_cli.py models list"

# nanobot 配置文件
exec command="cat ~/.nanobot/config.yaml 2>/dev/null || cat ~/.nanobot/config.json 2>/dev/null | python3 -m json.tool"
```

## 连通性测试

```bash
# 测试 API 连通
exec command="curl -s -o /dev/null -w '%{http_code} %{time_total}s' https://openrouter.ai/api/v1/models -H 'Authorization: Bearer $KEY' | head -1"

# DNS 解析
exec command="nslookup api.openai.com"

# 网络延迟
exec command="ping -c 3 api.openai.com"
```

## 文件系统

```bash
# workspace 内容
exec command="ls -la ~/.nanobot/workspace/"

# 磁盘用量
exec command="du -sh ~/.nanobot/*"

# 最近修改的文件
exec command="find ~/.nanobot -name '*.json' -mmin -10 -ls"
```

## 诊断流程

1. **先看日志** — `tail /tmp/nanobot.log` 找最近的错误
2. **检查进程** — 确认 nanobot gateway 在运行
3. **验证配置** — providers/models 是否正确
4. **测试连通** — API endpoint 是否可达
5. **汇报结果** — 用 message 工具告知用户问题和建议
