# nanobot 工程上下文（Grok Build）

这是用户的 **nanobot** 个人 AI 助手项目源码。

## 仓库

- 源码根目录: `/root/nanobot-src`
- 运行时配置/数据: `/root/.nanobot`（agents、sessions、logs）
- 主包: `nanobot/`（agent、channels、cli、providers、skills 等）

## 读代码前先看

- `README.md` — 功能、架构、安装
- `nanobot/agent/` — Agent 核心循环
- `nanobot/channels/` — Telegram/Discord/飞书等通道
- `nanobot/cli/` — 命令行入口
- `nanobot/providers/` — LLM 提供商

## 开发约定

- Python ≥3.11，轻量实现优先（项目定位是 ultra-lightweight）
- 测试: `pytest`（见 README / CONTRIBUTING）
- 改通道相关代码时注意 `~/.nanobot` 里的 live 配置

## Ponytail

写代码时遵循全局 Ponytail 规则：YAGNI、标准库优先、最少文件、不过度抽象。