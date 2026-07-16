# nanobot 工程上下文（Grok Build / agents）

源码根目录: `/root/nanobot-src`  
运行时配置/数据: `/root/.nanobot`（agents、sessions、logs、config.json）

## 读代码地图（按重构方向）

### 1. 仓库根（工程层）

| 路径 | 含义 |
|------|------|
| `nanobot/` | 可安装主包（真正业务代码） |
| `tests/` | pytest |
| `docs/` | 设计/审计文档（见 `docs/ARCHITECTURE.md`） |
| `scripts/` | 运维与开发脚本（原根目录 `tools/` 已并入） |
| `assets/` | 演示图、架构图、logo（非运行时） |
| `bridge/` | 可选 Node bridge |
| `memory-palace/` | 可选 Memory-Palace 子模块（原误名 `config/`） |
| `pyproject.toml` / Docker* | 打包与部署 |

**不要**把根目录 `scripts/` 和 `nanobot/tools/` 搞混：后者是 LLM 工具实现。

### 2. 主包 `nanobot/`（产品层）

| 路径 | 含义 |
|------|------|
| `groupchat/` | **多 agent 群聊主战场**（runtime / context / display） |
| `agent/` | 轻量单 agent / 框架向循环（与 groupchat 并列） |
| `channels/` | Telegram 等通道适配（产品表面） |
| `cli/` / `headless.py` | CLI 与 headless gateway 入口 |
| `providers/` | LLM 提供商 |
| `tools/` | 通用工具实现（read_file、exec、web…） |
| `core/` | 跨子系统原语，当前核心是 **`History`** |
| `nanobot/gateway/` | **网关** inbound 路由、inbox（**不是**群聊中间层） |
| ~~`nanobot/runtime/`~~ | 已移除；请用 `nanobot.gateway` |
| `nanobot/config/` / `session/` / `cron/` / … | 应用配置 schema、会话、定时等 |

### 3. 架构原则（必须遵守）

| 层 | 包 / 类 | 职责 | 禁止 |
|----|---------|------|------|
| **上下文（唯一）** | `nanobot.core.history.History` | 共享 transcript 的写入与处理：append、压缩、裁剪、`build_for_groupchat` / `build_for_llm`、`age_tools`、`commit_turn` | 在 runtime/display 另建平行长期消息库 |
| **协作 / 群聊** | `nanobot.groupchat.runtime` | 谁说话、打断、mailbox、round/cycle、tool_loop 调度、AgentRunner | 自己实现 compress/trim/长期 history |
| **视图** | `nanobot.groupchat.display` | 文案、流式、面板 | import runtime；改 History |
| **投影 / 策略** | `nanobot.groupchat.context` | 从 History **读出** prompt、rank 策略、settings、落盘 I/O | 冒充「第二套上下文」 |

**WorkingMemory**（在 runtime）= tool_loop 一轮内的 ephemeral 协议缓冲；re-entry 必须从 History 重建。

```text
用户/agent 产出
      │
      ▼
History.commit_turn / add_from_sender     ← 唯一 durable 上下文写入
      │
      ├─ engine._persist_after_history_write  （I/O，非上下文逻辑）
      │
      ▼
PromptBuilder(history) → LLM messages     ← 投影
      │
      ▼
WorkingMemory (ephemeral tool protocol)
      │
      ▼
display 渲染（runtime 调用，视图不写 History）
```

### 4. 群聊包地图

| 父级 | 角色 |
|------|------|
| **runtime/** | 协作中间层：`engine`, `broadcast`, `agent_cycle`, `mailbox`, `working_memory` |
| **context/** | History 的投影与策略：`prompt_builder`, `ranks`, `conversation`, settings |
| **display/** | 纯视图 |

### 5. 网关 vs 群聊协作层

| 符号 | 含义 |
|------|------|
| `nanobot.gateway` | 网关：slash、inbox |
| `nanobot.groupchat.runtime` | 群聊协作：engine / broadcast / mailbox |
| `nanobot.core.history.History` | **唯一上下文逻辑** |

历史包名已移除：`groupchat.orchestra`、`groupchat.history`、`nanobot.runtime`。

## 开发约定

- Python ≥ 3.11；轻量优先（YAGNI）
- 测试：`pytest`（`tests/`）
- 改通道/群聊时注意 `~/.nanobot` live 配置
- 改结构时更新 `docs/ARCHITECTURE.md`

## 详细架构

见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
