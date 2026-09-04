# nanobot Skills

This directory contains built-in skills that extend nanobot's capabilities.

## Skill Format

Each skill is a directory containing a `SKILL.md` file with:
- YAML frontmatter (name, description, metadata)
- Markdown instructions for the agent

**Agent Skills 标准兼容**（2026-09-04 起）：frontmatter 解析支持标准折叠/字面块
（`>-`、`|` 等）、可选字段（`license`、`allowed-tools`）和嵌套 `metadata:` map
（nanobot 专属键如 `always` 可直接写成嵌套 YAML，不必再塞 JSON 字符串）。
生态技能（如 superpowers / Anthropic skills）整个目录拷进 `skills/` 即可用；
PyYAML 缺席时自动降级到内置子集解析器（契约由 `tests/test_skill_frontmatter_compat.py` 钉死）。

## Attribution

These skills are adapted from [OpenClaw](https://github.com/openclaw/openclaw)'s skill system.
The skill format and metadata structure follow OpenClaw's conventions to maintain compatibility.

## Available Skills

| Skill | Description |
|-------|-------------|
| `github` | Interact with GitHub using the `gh` CLI |
| `weather` | Get weather info using wttr.in and Open-Meteo |
| `summarize` | Summarize URLs, files, and YouTube videos |
| `tmux` | Remote-control tmux sessions |
| `clawhub` | Search and install skills from ClawHub registry |
| `skill-creator` | Create new skills |