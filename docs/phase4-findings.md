# Phase 4 清理遗留代码 — 发现与结论

> 创建日期: 2026-09-07
> 状态: 审计完成，部分安全改动已应用

## 4.1 循环依赖审计

### 发现

对 `nanobot/` 全量扫描，真正为打破循环而存在的 lazy import **只有一处**：

- `nanobot/groupchat/orchestra/engine.py:118`
  ```python
  # Lazy import to avoid circular: engine → agent.tools → agent → config → groupchat
  from nanobot.tools.registry import ToolRegistry
  ```

其余 lazy import 均为**防御性/初始化顺序**目的，非真正循环：

| 位置 | 原因 | 是否真正循环 |
|------|------|-------------|
| `engine.py` 多处 `_build_tool_registry` | 工具按需加载，避免模块加载时重导入 | 否 |
| `tool_loop.py` history 设置 import | 同包内，避免加载顺序问题 | 否 |
| `events.py` 模块级单例 | 懒构造默认 bus | 否 |
| `mods/registry.py` builtin 包扫描 | 插件发现 | 否 |

### 结论

**4.1 无需重新组织。** `tools/` 不反向 import `groupchat/`（已验证），不存在包级循环。engine.py:118 的 lazy import 是必要的，保留。引入"中间层"反而增加复杂度，无收益。

## 4.2 Session vs HistoryContext

### 发现

两者**并非重复**，服务不同模式且消息 schema 不同：

| 维度 | `Session` | `HistoryContext` |
|------|-----------|------------------|
| 位置 | `nanobot/session/manager.py` | `nanobot/groupchat/history/context.py` |
| 模式 | 直接聊天 / 单 agent | 群聊 / 多 agent |
| 消息 schema | role-based（`role`/`content`/`tool_calls`，OpenAI 格式） | sender-based（`sender`/`content`） |
| 持久化 | 每会话 JSONL 文件 | `GroupChatState` |
| 边界对齐 | `_find_legal_start`（tool_call 配对） | 头保护 + 压缩 |
| 消费者 | direct_chat / CLI | broadcast / groupchat |

### 结论

**4.2 不合并。** 统一两者需要迁移其中一种消息 schema（高风险，影响所有 LLM 调用），且收益不明确——它们本就服务不同场景。已在两个类的 docstring 中明确职责区分。原 plan 假设的"重复"不成立。

## 4.3 删除 `engine._running` 遗留 flag

### 发现

`engine._running` 当前承担**双重职责**：

1. **会话级**：`run_loop.py` 的 `while engine._running:` 是会话主循环条件。
   - `start_group_chat()` 设 True（engine.py:905）
   - 退出时设 False（engine.py:941, run_loop.py:183）
   - `run_loop.py:159-164` 在 end_discussion 后若有 pending 消息会"复活"它

2. **轮次级**：`RoundLifecycle.mark_winding_down(flip_running=True)` 翻 False；
   `reopen()` 翻 True。

这正是 AGENTS.md 警告的"状态无主"问题——同一个 flag 混用两个语义。
`RoundLifecycle` 已接管**轮次级**状态（ACTIVE/WINDING_DOWN/ENDED），但
`run_loop.py` 仍直接读 `engine._running` 作为**会话级**条件，迁移未完成。

### 当前调用点（运行时读写，排除注释）

- `run_loop.py`: 111, 116, 123, 159, 164, 183（会话级）
- `engine.py`: 291, 329, 450, 504, 859, 861, 871, 873, 878, 903, 905, 941
- `broadcast.py`: 1553（`not engine._running` — join listener）
- `chatroom_tools.py`: 1199（`self._engine._running = False`）
- `round_lifecycle.py`: 91, 113（flip_running 写入）

### 结论

**4.3 不完全删除。** 完全移除需要把 `run_loop.py` 的会话循环条件迁离 `engine._running`，
属于高风险改动（影响会话生命周期），需要先写回归测试钉住行为。本次只做：

- 在 `engine._running` 声明处加迁移说明注释（已完成）
- 明确：新代码不应直接读 `engine._running`，改用 RoundLifecycle 查询
  （`agents_should_exit` / `accepts_interjection` / `session_should_stop`）

### 后续迁移路径（未来工作）

1. 为 `run_loop.py` 会话循环写集成测试（钉住"end_discussion 后 pending 消息复活"行为）
2. 引入 `session_active` 状态源（会话级），与 RoundLifecycle（轮次级）解耦
3. 逐步替换 `run_loop.py` / `broadcast.py:1553` 的 `engine._running` 读取
4. 删除 `flip_running` 参数，`engine._running` 属性

---

## 本阶段已应用的改动

| 文件 | 改动 | 风险 |
|------|------|------|
| `nanobot/session/manager.py` | Session docstring 明确职责 | 无（仅注释） |
| `nanobot/groupchat/history/context.py` | HistoryContext docstring 明确职责 | 无（仅注释） |
| `nanobot/groupchat/orchestra/engine.py` | `_running` 声明处加迁移说明 | 无（仅注释） |

## 参考红线（AGENTS.md）

- #2 禁止止血式补丁堆积——修根源不加第 N 个护栏 flag
- #7 修不动就停手汇报——不带着未验证的改动继续堆叠

Phase 4 的三项在审计后判断：**4.1 无需改动，4.2 不应合并，4.3 完全删除风险过高**。
本次仅应用零风险文档性改动，并将完整迁移路径留作未来工作。
