# nanobot 架构地图

本文对齐 **2026-07 重构方向**：视图层 · 中间松散逻辑层 · 上下文层。  
旧文档（如 `groupchat-coupling-fix.md`）中的 `orchestra` / `HistoryContext` 名称以本文与代码为准。

## 1. 仓库布局

```text
nanobot-src/
  nanobot/           # 主 Python 包
  tests/
  docs/              # 含本文件
  scripts/           # 开发/运维脚本
  assets/            # 图示与演示素材（非运行时）
  bridge/            # 可选 Node bridge
  pyproject.toml
  Dockerfile, docker-compose.yml
```

| 勿混淆 | |
|--------|--|
| `scripts/` | 仓库脚本 |
| `nanobot/tools/` | Agent 可调用的工具实现 |
| `nanobot/config/` | 应用配置 schema/路径 |
| 仓库根曾用的 `config/` | 若存在，视为独立部署栈，不是 `nanobot.config` |

运行时数据在 **`~/.nanobot`**，不在源码树。

## 2. 主包模块职责

```text
nanobot/
  core/              # History 等跨子系统原语
  groupchat/         # 多 agent（见下节三层）
  agent/             # 轻量 agent 路径
  channels/          # Telegram / 其它通道
  gateway/           # 网关：dispatch + inbox（原 nanobot.runtime，现为主名）
  # runtime/         # 已移除（曾 shim → gateway）
  providers/         # LLM
  tools/             # 工具实现
  cli/ + headless.py # 入口
  …
```

### 网关 vs 群聊 `runtime`

| 包 | 职责 | 典型入口 |
|----|------|----------|
| `nanobot.gateway` | 网关：统一 slash 路由、inbox 文件轮询 | `dispatch.py`, `inbox.py` |
| `nanobot.groupchat.runtime` | 群聊中间逻辑：一轮多 agent、mailbox、WorkingMemory | `engine.py`, `broadcast.py` |
| ~~`nanobot.runtime`~~ | 已移除 → 使用 `nanobot.gateway` | — |

## 3. 群聊三层（核心）

```text
nanobot/groupchat/
  display/     # 视图：消息排版、流式编辑、状态面板文案
  runtime/     # 逻辑：何时跑、打断、tool_loop、commit/refresh
  context/     # 上下文：prompt 组装、persistence、ranks、pruning 设置
```

### 3.1 视图 `display`

- **做**：把事件变成用户可见文本；StreamingDisplay；BroadcastView 渲染
- **不做**：import `groupchat.runtime`；不直接改 History / busy
- **约定**：需要副作用时由 runtime **注入回调**（如 `on_chatroom_send_ok`）
- **守卫**：`tests/test_display_no_runtime_import.py`

### 3.2 中间逻辑 `runtime`

- **做**：`GroupChatEngine`、`broadcast_round`、`MailboxHub`、`AgentRunner`、`WorkingMemory`、`tool_loop`
- **busy 写路径**：仅 `AgentRunner.begin_cycle/end_cycle`
- **上下文写路径**：`commit_agent_turn` → `engine.history`（`core.history.History`）
- **re-entry**：wait / interrupt / system nudge → `WorkingMemory.refresh` 从 History 重建
- **不做**：在 display 里实现打断策略；rank 规则不从 display 取

相关模块：

| 文件 | 角色 |
|------|------|
| `broadcast.py` | 一轮多 agent 入口 + per-agent cycle 体 |
| `broadcast_orchestrator.py` | 一轮资源/工具/池 setup |
| `broadcast_status.py` | 状态面板 tracker |
| `working_memory.py` | ephemeral LLM session |
| `mailbox.py` | 消息队列 + interrupt 事件 |
| `agent_runner.py` | busy/idle + cancel 契约 |

### 3.3 上下文 `context`

- **做**：`PromptBuilder`、snapshot 持久化、`history_settings`、`tool_pruning`、**`ranks`（等级/可见性策略）**
- **真相对象**：`nanobot.core.history.History`（Fragment 列表，非本包内另一套 list）
- **不做**：持有 asyncio task / interrupt Event

| 文件 | 角色 |
|------|------|
| `prompt_builder.py` | 系统组件 + `History.build_for_groupchat` |
| `ranks.py` | resolve_rank、compute_agent_ranks、can_see… |
| `persistence.py` | GroupChatState 落盘 |
| `history_settings.py` | 压缩/窗口等旋钮 |

### 3.4 Legacy shim

| 旧路径 | 指向 |
|--------|------|
| ~~`groupchat.orchestra.*`~~ | `groupchat.runtime.*`（shim 已删） |
| ~~`groupchat.history.*`~~ | `groupchat.context.*`（shim 已删） |
| `display.visibility` 策略 API | 再导出 `context.ranks`（`tool_call_label` 仍在 display） |

新代码请直接 import **runtime / context / display** 与 **core.history**。

## 4. 关键数据流（多 agent）

```text
用户消息 → channels → engine
                ↓
         History.add_from_sender
                ↓
         broadcast_round / _run_one
                ↓
    WorkingMemory ← build_agent_prompt(History)
                ↓
            tool_loop …
                ↓
    commit_agent_turn → History
                ↓
    wait/interrupt/nudge → WorkingMemory.refresh(History)
                ↓
         display 渲染（由 runtime 调用）
```

## 5. 测试护栏（结构相关）

| 测试 | 约束 |
|------|------|
| `test_display_no_runtime_import.py` | display 不 import runtime |
| `test_runtime_ranks_not_from_display.py` | runtime 不从 display.visibility 取 rank 策略 |
| `test_working_memory.py` | commit / refresh 语义 |
| `test_history.py` | core History API |

## 6. 演进备忘

- ~~网关改名 gateway~~ 已完成；`nanobot.runtime` shim 已删除
- `broadcast.py` 内嵌 `_run_one` 仍长 → 可再抽 `agent_cycle` 模块
- 删除 `orchestra/`、`history/` 空壳前确认无外部硬依赖

## 7. 相关旧文档

| 文件 | 状态 |
|------|------|
| `docs/groupchat-coupling-fix.md` | 卡死诊断仍有用；模块路径已偏旧 |
| `docs/groupchat-state-concept-map.md` | 状态消歧仍有用；以本文包名为准 |
| `docs/GROUPCHAT_CALL_CHAIN.md` | 调用链概览；orchestra 应读作 runtime |
