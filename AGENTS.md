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
| `nanobot/runtime/` | **废弃 shim** → `nanobot.gateway` |
| `config/` / `session/` / `cron/` / … | 配置路径、会话、定时等 |

### 3. 群聊 `nanobot/groupchat/`（三层）

| 父级 | 角色 | 典型内容 |
|------|------|----------|
| **runtime/** | **中间逻辑层**：调度、mailbox、round、runner | `engine.py`, `broadcast.py`, `mailbox.py`, `working_memory.py` |
| **context/** | **上下文层**：prompt、持久化、rank 策略、裁剪 | `prompt_builder.py`, `persistence.py`, `ranks.py` |
| **display/** | **视图层**：文案、流式、面板 | `display.py`, `streaming.py`, `broadcast_view.py` |

依赖方向：

```text
display  ✗→  runtime     （禁止；用回调注入）
runtime  →   context     （build prompt / commit History）
runtime  →   display     （渲染）
context  →   core.history.History
```

Legacy shim（勿再扩展）：

- `groupchat/orchestra/` → `groupchat/runtime/`
- `groupchat/history/` → `groupchat/context/`

### 4. 网关 vs 群聊逻辑层（勿混）

| 符号 | 含义 |
|------|------|
| `nanobot.gateway` | 网关：slash 命令表、inbox 文件投放 |
| `nanobot.groupchat.runtime` | 群聊中间逻辑：engine / broadcast / mailbox |
| `nanobot.runtime` | **废弃 shim** → 再导出 gateway |

新代码：网关用 **`nanobot.gateway`**；群聊逻辑用 **`nanobot.groupchat.runtime`**。

### 5. 上下文真相

- 共享 transcript 对象：`nanobot.core.history.History`
- 群聊内 WorkingMemory（ephemeral）在 `groupchat.runtime.working_memory`
- cycle 结束 `commit_agent_turn` → History；re-entry 从 History `refresh`

## 开发约定

- Python ≥ 3.11；轻量优先（YAGNI）
- 测试：`pytest`（`tests/`）
- 改通道/群聊时注意 `~/.nanobot` live 配置
- 改结构时更新 `docs/ARCHITECTURE.md`

## 详细架构

见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
