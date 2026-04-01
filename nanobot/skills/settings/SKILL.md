---
name: settings
description: "Manage providers, models, and system settings. Use when adding API providers, registering models, or checking configuration."
always: false
---

# Settings — 提供商与模型管理

管理 API 提供商和模型注册。

## CLI

```bash
python3 {baseDir}/scripts/settings_cli.py <resource> <action> [options]
```

## Providers

```bash
# 列出
python3 {baseDir}/scripts/settings_cli.py providers list

# 添加 (需要 name, url, key)
python3 {baseDir}/scripts/settings_cli.py providers add --name openrouter --url https://openrouter.ai/api/v1 --key sk-xxx

# 编辑 (改 url 或 key)
python3 {baseDir}/scripts/settings_cli.py providers edit --name openrouter --url https://new-url.com/v1
python3 {baseDir}/scripts/settings_cli.py providers edit --name openrouter --key sk-newkey

# 删除 (同时删除其下所有模型)
python3 {baseDir}/scripts/settings_cli.py providers remove --name openrouter
```

## Models

```bash
# 列出
python3 {baseDir}/scripts/settings_cli.py models list

# 添加 (指定提供商和模型 ID)
python3 {baseDir}/scripts/settings_cli.py models add --provider openrouter --model anthropic/claude-sonnet-4-5

# 删除
python3 {baseDir}/scripts/settings_cli.py models remove --provider openrouter --model anthropic/claude-sonnet-4-5
```

## 常见模型 ID

| Provider | Model ID |
|----------|----------|
| OpenRouter | anthropic/claude-sonnet-4-5, x-ai/grok-4.1-fast, google/gemini-2.5-flash |
| xAI | grok-4, grok-4-mini |
| SambaNova | Meta-Llama-3.3-70B-Instruct |
