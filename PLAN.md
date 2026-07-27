# 架构重构计划 (PLAN)

> 基于 2026-07 全量架构审查。核心结论：**系统脆弱的根源不是某个模块写得差，
> 而是 (a) 测试防护网已失效 (b) 模块边界全面泄漏 (c) UI 语义烧死在核心层。**
> 按 Phase 顺序执行，每个 Phase 独立可交付、可回归。
>
> 状态标记: ✅ 已完成 | 🚧 进行中 | ❌ 未开始

---

## 现状诊断摘要

| # | 问题 | 证据 | 严重度 |
|---|------|------|--------|
| D1 | 测试套件失效 | 10 个测试模块 import 已不存在的 `nanobot.groupchat.middleware`；另有 52 个测试因 telegram 内部重构失败。无回归防护 → "改改就崩" | 🔴 致命 |
| D2 | 私有属性横穿三层 | `broadcast.py` 摸 `engine._*` 67 处；`channels/telegram/callbacks.py`+`settings.py` 直接读写 `engine._history/_leader/_active_agents/_state` 共 30+ 处 | 🔴 致命 |
| D3 | UI 语义烧死在核心 | engine 持有 `_edit_fn/_send_and_get_id_fn`（Telegram 消息 ID+编辑模型）；其他 channel 无法接入群聊；display.py 的 UI 字符串与核心混杂 | 🟠 高 |
| D4 | 双轨历史/记忆系统 | `session/manager.py`（398 行+多个测试）在 AgentLoop 删除后仅剩 heartbeat 挑目标一个调用点，近似死代码；groupchat 另有 `HistoryContext`+`GroupChatState`；记忆双轨（MEMORY.md vs memory_palace/chroma）互不感知 | 🟠 高 |
| D5 | 对话池状态机脆弱 | ConversationPool 的 slot 经济学（按接收者扣 slot/wait 归还/超时回收）近期连续 3 个 deadlock/泄漏 fix commit；SearchPool 积分转账复杂度超过收益 | 🟠 高 |
| D6 | 巨型文件 | callbacks.py 2910 行（75 个 startswith 前缀路由）、broadcast.py 1698、engine.py 1677 | 🟡 中 |
| D7 | 状态存储位置混乱 | `GroupChatState` 硬编码写 `~/.nanobot/`（非 workspace）；prompt_builder 模块级文件 IO；history_settings 在函数内 lazy import + except 兜底默认值 | 🟡 中 |
| D8 | 消息总线双轨 | telegram 命令直连 gc_engine，bus 只走 outbound；inbound 半绕过 → 两条消息路径 | 🟡 中 |
| D9 | 空目录/孤儿引用 | `nanobot/agent/` 为空目录；注释/文档仍大量引用已删除的 AgentLoop | 🟢 低 |

### 各子系统"是否有用"判定

- **群聊系统（engine/broadcast/mailbox）**：✅ 核心价值所在，保留。但 broadcast.py 需拆分，engine 需公共 API 化。
- **ConversationPool（slot 经济学）**：⚠️ 概念有用（防单 agent 刷屏），实现过度。简化为「每轮发言预算 + asyncio.Semaphore」，删除 slot 归还/转账等边角逻辑。
- **MailboxHub**：✅ 有用，保留（agent 间定向/广播消息是群聊的基础能力）。
- **SearchPool 积分转账（transfer）**：❌ 花活，使用率存疑，降级为纯配额（保留 spend/status，删 transfer）。
- **SessionManager**：⚠️ 设计不差但已成孤儿。二选一：(a) 让 groupchat 历史迁移到它上面统一持久化 (b) 删除并把 heartbeat 目标选择改读 GroupChatState。倾向 (b)——groupchat 已是唯一执行路径。
- **memory_palace + MEMORY.md 双记忆**：⚠️ 需统一入口。MEMORY.md 作为"始终注入的长期事实"，memory_palace 作为"可检索大容量记忆"，中间缺一条 consolidation 管线（对话压缩产物应落入 palace，摘要精华落入 MEMORY.md）。
- **display/StreamingDisplay/AgentStatusTracker**：✅ 有用，但需抽象出 `ChatUI` 接口使其 channel 无关。

---

## Phase 0 — 修复防护网（先于一切重构）✅

> 没有绿色测试基线，后续任何重构都是裸奔。预计 1-2 天。

- [x] **P0.1** 修复 10 个 `middleware` import 错误的测试模块：改为 `nanobot.groupchat.history.*` 路径；断言不再成立的测试逐个判断是"改断言"还是"删除"（功能已不存在的删，行为变化的改）。
  - 涉及: test_context_prompt_cache / test_context_pruning / test_groupchat / test_loop_consolidation_tokens / test_loop_save_turn / test_matrix_channel / test_memory_consolidation_types / test_message_tool_suppress / test_prompt_robustness / test_summarizer
- [x] **P0.2** 修复 52 个属性缺失类失败（test_telegram_channel 22 / test_commands 11 / test_task_cancel 8 / test_consolidate_offset 4 / test_restart_command 3 / test_config_migration 2 / 其他 2）。原则同上：跟随现实现，不为测试恢复死代码。
- [x] **P0.3** 加 `scripts/check.sh`：`pytest -q` + `ruff check`（先只开 F/E 级），并写入 CONTRIBUTING。之后每个 Phase 结束必须全绿。
- [x] **P0.4** 删除空目录 `nanobot/agent/`，清理注释中的 AgentLoop 孤儿引用（保留 tool_loop.py 中对历史的说明性提及可改写）。

**验收**: `pytest` 0 error 0 fail；后续 PR 均以此为门禁。

---

## Phase 1 — 引擎公共 API 化，封死边界泄漏 ✅

> 目标：`engine._xxx` 在 groupchat 包外出现次数 = 0。这是消除"改一处崩三处"的关键。

- [x] **P1.1** 在 `GroupChatEngine` 上定义正式公共 API（只读属性 + 变更方法）：
  - 只读: `history_stats() -> (msgs, chars)`, `active_agents`, `leader`, `is_running`, `registry_names`, `input_queue_size`, `request_log_size`, `topic`
  - 变更: `rename_agent(old, new)`（内部处理 active/leader/groups/持久化的一致性——目前 callbacks.py 手动做 6 步同步，就是 bug 温床）, `resolve_agent(name)`, `set_debug_context(bool)`
- [x] **P1.2** 迁移 `channels/telegram/callbacks.py`（15 处）与 `commands/settings.py`（19 处）到公共 API；grep 门禁：`grep -rn "engine\._" nanobot/channels` 必须为 0。
- [x] **P1.3** 收紧 `BroadcastContext` Protocol：把 broadcast 需要的 67 处 `engine._*` 全部提升为 Protocol 上的显式成员（`send()`, `add_message()`, `save_event()` 等公共命名），engine 实现之。broadcast 内禁止出现 `._` 访问 engine。
- [x] **P1.4** 解开 engine ↔ broadcast 循环依赖：`build_tool_log` / `log_request` 移入独立模块（如 `orchestra/request_log.py`），双方各自 import。
- [x] **P1.5** 为公共 API 补单测（rename_agent 的一致性、active/leader 持久化往返）。

**验收**: 包外无 `_` 访问；engine/broadcast 无循环 import；测试绿。

---

## Phase 2 — UI 抽象层：让群聊与 Telegram 解耦 ❌

> 目标：groupchat 核心只依赖一个 `ChatUI` 接口，任何 channel 实现该接口即可接入群聊。

- [x] **P2.1** 定义 `ChatUI` Protocol（放 `groupchat/display/ui.py`）：
  ```python
  class ChatUI(Protocol):
      async def send(self, text: str) -> int | None: ...
      async def edit(self, msg_id: int | None, text: str) -> None: ...
      @property
      def supports_edit(self) -> bool: ...
  ```
  不支持编辑的 channel（如 email）degrade 为追加发送。
- [x] **P2.2** engine 的 `set_send_fn/set_edit_fn/_send_and_get_id_fn` 三件套收敛为 `set_ui(ui: ChatUI)`；向后兼容旧 API。
- [x] **P2.3** TelegramChannel 提供 `TelegramChatUI` 适配器（含现有 msg_id 语义、消息拆分、429 退避）。
- [x] **P2.4** display.py 拆分：纯格式化函数（emoji/文案）保持；把「何时发/何时编辑」的流程逻辑归 StreamingDisplay/Tracker，格式与流程不再互相 import 交叉。
- [x] **P2.5** （可选验证）给 feishu 或 CLI 写一个最小 ChatUI 实现，证明群聊可跨 channel。

**验收**: groupchat 包内无 telegram 概念（msg_id int、parse_mode 等）；Telegram 行为不回归。 ✅

---

## Phase 3 — 拆巨型文件 + 回调路由表 ❌

- [ ] **P3.1** `callbacks.py`（2910 行）拆为 `callbacks/` 包：`router.py`（前缀路由表 `CALLBACK_ROUTES: dict[str, Handler]`，最长前缀匹配）+ `agent_ops.py` / `hyperparams.py` / `prompt_edit.py` / `provider_models.py` / `logs.py`。已有的 `_handle_*` 方法是现成拆分线。
- [ ] **P3.2** 路由表单测：断言所有前缀无歧义（`rlog:` vs `rlogctx:` 类冲突静态检出）。
- [ ] **P3.3** `broadcast.py`（1698 行）拆为：`status_tracker.py`（AgentStatusTracker）✅、`interrupts.py`（realtime interrupt + listeners）✅、`round.py`（broadcast_round 主体）、`agent_task.py`（`_run_one` 单 agent 执行）。
- [ ] **P3.4** `engine.py`（1677 行）瘦身：direct_chat 相关移入 `direct_chat.py`；工具注册移入 `tools/setup.py`；engine 只留状态管理 + 生命周期 + 公共 API。

**验收**: 无单文件 >900 行（channels 的 SDK 封装除外）；行为无变更；测试绿。

---

## Phase 4 — 对话池简化 ❌

> 目标：把"精巧但脆弱"的 slot 经济学换成可推理的简单模型，消除 deadlock 类 bug 的土壤。

- [ ] **P4.1** ConversationPool 重设计：
  - 保留语义：每 agent 每轮有发言预算（防刷屏）；用户消息永不阻塞。
  - 删除：slot 归还（wait 不回复退 slot）、跨 agent 记账、15s allocate 超时等边角状态。
  - 新实现：`per_agent_budget: dict[str, int]` + 每轮重置 + 超预算的 send 直接返回工具错误（让 LLM 自己收敛），**不阻塞不等待** → 从根上消灭 deadlock。
- [ ] **P4.2** MailboxHub 保留，补充不变量断言与单测（广播、定向、无接收者、engine 停止时清空）。
- [ ] **P4.3** SearchPool 降级：保留 spend/status/per-agent 配额，删除 `transfer`（积分转账）；`CachedSearchTool` 缓存保留；`SmartSearchTool`（nano 模型摘要）标记 experimental，默认关闭。
- [ ] **P4.4** 为新 pool 写属性化测试（并发 send 压测不悬挂）。

**验收**: `mailbox.py` 从 807 行显著缩减；压测无悬挂；群聊行为主观不劣化。

---

## Phase 5 — 历史/记忆系统统一 ❌

- [ ] **P5.1** 处置 SessionManager：确认 groupchat 是唯一执行路径后，heartbeat 的 `_pick_heartbeat_target` 改从 GroupChatState/channel 活跃记录取目标，然后删除 `session/`（连同其测试）。若未来要恢复 1-on-1 AgentLoop，从 git 历史找回。
  - [待确认] 是否有外部脚本依赖 `~/.nanobot/sessions/` 文件格式。
- [ ] **P5.2** GroupChatState 存储位置迁移：`~/.nanobot/` → `workspace/.groupchat/`（含迁移逻辑：启动时检测旧路径自动搬迁）。消除"全局单例状态导致多 workspace 互相污染"。
- [ ] **P5.3** history_settings 去全局化：函数内 lazy import + except 兜底改为启动时注入 `HistorySettings` 对象（engine 持有，broadcast 经 Protocol 读取），配置热更新时整体替换。
- [ ] **P5.4** 记忆统一管线（分两步）：
  1. 定义 `MemoryStore` 接口：`store(text, kind)` / `recall(query, k)` / `digest()`；memory_palace 为默认实现，MEMORY.md 为 digest 层（少量高价值事实，始终注入 prompt）。
  2. 接入 `HistoryContext.maybe_compress()`：压缩掉的中段历史 → 自动 `store()` 入 palace；摘要 → 追加候选到 MEMORY.md（或提示 agent 复核）。让"遗忘"变成"归档"。
- [ ] **P5.5** prompt_builder 的 manifest/labels 模块级 IO 收敛进 PromptBuilder 实例（消除 import 副作用残留风险）。

**验收**: 单一持久化根目录；记忆写入/召回单测；多 workspace 并存不串数据。

---

## Phase 6 — 消息路径统一 + 收尾 ❌

- [ ] **P6.1** inbound 路径统一：telegram 命令处理仍可直连 engine（低延迟 UI 操作合理），但**用户对话消息**统一走 bus → engine 订阅，使 feishu/discord 等未来接群聊时无需复制 telegram 的直连逻辑。
- [ ] **P6.2** `except Exception` 治理续（todo.md #4 遗留）：callbacks.py 剩余 28 处静默吞错在 P3.1 拆文件时顺手补日志；启用 ruff `S110`。
- [ ] **P6.3** shell 强隔离模式（todo.md #3 遗留）：单独立项，评估 bwrap。
- [ ] **P6.4** 文档：`docs/ARCHITECTURE.md` 画清最终分层（channels → ChatUI/bus → engine → history/memory/tools），并声明"包外禁止访问下划线成员"为硬规则（加 CI grep 门禁）。

---

## 执行原则

1. **Phase 0 不做完不动任何重构**——先有绿灯再动手术。
2. 每个 Phase 一个分支、一次合入，合入前 `scripts/check.sh` 全绿。
3. 重构 Phase（1/2/3）**零行为变更**；行为变更 Phase（4/5）先写目标行为测试。
4. 不臆猜：P5.1 的 [待确认] 项动手前先和用户对齐。
5. 巨改期间冻结新功能；确需插入的新功能只允许建立在已完成 Phase 的新边界上。

## 依赖关系

```
P0 ──► P1 ──► P2 ──► P3
        │             │
        └──► P4       └──► P6
        └──► P5
```
P4/P5 依赖 P1 的边界（Protocol/公共 API），可与 P2/P3 并行。
