# nanobot-src 架构重构计划 — 状态所有权 / broadcast.py 拆分 / channels 收敛

> 创建日期: 2026-09-07
> 状态: **规划中，未开始实现**
> 范围: 三个阶段，有严格依赖顺序（Phase 1 是 Phase 2 的前置条件）
> 上一轮计划: 群聊历史模型重构（Phase A-E，已完成）见 `docs/archive/plan-2026-09-07-history-refactor.md`
> 红线遵循: AGENTS.md #1 先写测试再改实现、#2 修根源不堆护栏、#4 删死代码、
>   #5 编辑前重读、#6 每逻辑单元 checkpoint 提交、#7 修不动就停手汇报

## Context — 为什么做这次重构

历史模型重构（Phase A-E）完成后做了一次独立架构审查（只读调查，未改代码），
覆盖 `AGENTS.md`/`docs/phase4-findings.md`/上一版 `plan.md` 未讲透的部分。核心发现：

**AGENTS.md 宣称的"RoundLifecycle 是轮次状态唯一归属"这条不变量，代码层面不成立。**
`round_lifecycle.py` 的 docstring 自己承认它只是给"未迁移的读者"做的兼容翻译层
（`round_lifecycle.py:4-11`）：`mark_winding_down(flip_running=True)` 直接赋值
`self._engine._running = False`（:91），`reopen()` 直接赋值 `= True`（:113）。
真正的状态位仍是 `engine.py:295` 的裸 `bool`，且读写已经**渗出 orchestra 包之外**——
`channels/telegram/__init__.py:544`、`channels/telegram/commands/settings.py:297`
也在直接戳 `engine._running`。全仓库 6 个文件、20+ 处直接读写。这正是
AGENTS.md #2 点名的"状态无主"问题（补丁净增 3:1 的历史成因），且比
`docs/phase4-findings.md` 描述的"迁移未完成"更严重——不是部分完成待收尾，
而是这轮重构完全没触碰它。

**`broadcast.py`（1875 行）里 `broadcast_round` 是一个 ~1500 行的巨函数**，
内嵌 4 层闭包（`_run_one` → `_on_tool_start`/`_on_tool_result`/`_inject_retry`/
`_on_iter_usage`/`_badge`，加上同级 `_user_listener`/`_join_listener`/
`_watch_leader_end`/`_watch_no_leader_convergence`），闭包间靠共享外层局部变量
通信，无法单独单元测试。它是唯一同时 import `engine`/`mailbox`/`round_lifecycle`/
`chatroom_tools`/`user_ingress`/`display` 六个子系统的节点，是耦合度最高的单点。

**`channels/`（38 文件/12701 行，全仓库最大模块）复用不足**：公共基础设施薄
（`base.py` 139 行，`channels/utils/` 397 行），`feishu.py`(1247)/`mochat.py`(946)/
`matrix.py`(738)/`dingtalk.py`(585) 各自手写轮询循环、去重、媒体处理的变体。
`mochat.py` 946 行**零专属测试**，风险最高。

**根因**：三个问题都指向同一件事——之前的重构只解决了"历史数据模型"的契约化，
没碰"控制流状态"的契约化。`engine._running` 的裸共享 bool 和 `broadcast_round`
的巨函数闭包，本质上是同一类问题的两个表现：状态和控制流散落在没有边界的
共享可变环境里，而不是被显式对象持有、显式传递。

---

## 依赖顺序（不可颠倒）

```
Phase 1: 状态所有权收编 ──► Phase 2: broadcast_round 拆分
                                        │
Phase 3: channels/ 收敛（风险最低，可与 Phase 1/2 并行）
```

**为什么 Phase 1 必须先做**：如果先拆 `broadcast_round`，"状态无主"的 bug 只会
被拆分到更多文件里，反而更难追踪——拆分需要干净的状态边界才有意义。Phase 3
不涉及 orchestra 核心状态机，风险独立，可以并行推进不阻塞主线。

---

## 现有代码关键事实（已逐行核实）

| 事实 | 位置 | 含义 |
|------|------|------|
| `engine._running` 声明处，注释承认双重语义未解决 | `engine.py:287-295` | 会话级 + 轮次级共享一个 bool |
| `round_lifecycle.py` 是翻译层非真正状态源 | `round_lifecycle.py:4-11,91,113` | `flip_running=True` 直接改写裸 bool |
| `run_loop.py` 会话主循环条件直读 `_running` | `run_loop.py:111,116,123,159,164,183` | 会话级消费方，含"pending 消息复活"逻辑 |
| `broadcast.py` 多处读写 `_running` | `broadcast.py:721,974,1553,1701,1810` | 轮次级消费方 + leader 崩溃/超时分支 |
| `chatroom_tools.py` 直接赋值 | `chatroom_tools.py:1210,1214` | `ChatroomEndDiscussionTool` |
| **渗出 orchestra 包外** | `channels/telegram/__init__.py:544` | 读 `_groupchat_engine._running` 判断是否运行中 |
| **渗出 orchestra 包外** | `channels/telegram/commands/settings.py:297` | 调试命令打印 `engine._running` |
| `broadcast_round` 函数跨度 | `broadcast.py:377` 起，至文件尾 ~1875 | 单函数吞掉文件 ~80% |
| `BroadcastOrchestrator` 已存在但职责窄 | `broadcast.py:218-259` | 目前只管 `setup_tools_and_pools` |
| `_run_one` 内嵌 4 层闭包 | `broadcast.py:491,647,653,712,802,1094` | 无法独立测试 |
| 监听器闭包与主流程共享局部变量 | `broadcast.py:1517,1543,1641,1652` | `_user_listener`/`_join_listener`/`_watch_leader_end`/`_watch_no_leader_convergence` |
| `channels/base.py` 抽象薄 | `channels/base.py`（139 行） | 多数 channel 不复用模板方法 |
| `mochat.py` 零专属测试 | `nanobot/channels/mochat.py`（946 行） | `tests/` 无 `test_mochat*.py` |
| `discord.py`/`wecom.py`/`whatsapp.py`/`manager.py`/`registry.py` 无专属测试 | `nanobot/channels/` | 只在 `test_channel_plugins.py`/`test_status_panel.py` 间接覆盖 |

---

## Phase 1：状态所有权收编

**目标**：把会话级和轮次级状态从共享裸 `bool` 拆成两个显式状态源，
`engine._running` 退役（或降级为只读兼容属性），channels 层不再直接碰
orchestra 内部状态。

1. **先写回归测试钉住当前行为**（AGENTS.md #1，先测试再动实现）：
   - `run_loop.py:159-164`："end_discussion 后 pending 消息复活"——这是当前
     行为里最隐蔽的一处，依赖 `_running` 的会话级语义，必须先有测试再动。
   - `broadcast.py:974,1701,1810` 的 `mark_winding_down(..., flip_running=True)`
     三处调用（leader 崩溃 / 全局超时 / 全局超时兜底）——确认轮次结束后
     会话级状态的正确转换。
   - `channels/telegram/__init__.py:544` 的"群聊已停止"提示文案依赖的判断逻辑。

2. **引入显式会话级状态源**（新对象，例如 `SessionState`，与 `RoundLifecycle`
   同级但语义分离）：
   - 会话级：是否仍在消费 `_input_queue`（当前 `run_loop.py` 的循环条件）。
   - 轮次级：继续由 `RoundLifecycle` 持有（`ACTIVE`/`WINDING_DOWN`/`ENDED`），
     但**不再通过写 `engine._running` 来对外广播**——改为暴露显式查询方法
     （`accepts_interjection`/`agents_should_exit`/`wait_should_exit`/
     `session_should_stop`，这些已存在，扩展即可，不用新造轮子）。

3. **逐个迁移读写点**（每改一处跑相关测试子集，不要攒大 diff）：

   | 当前访问 | 迁到 |
   |----------|------|
   | `run_loop.py` 循环条件 `while engine._running` | `session_state.is_active` |
   | `broadcast.py:1553` `not engine._running`（join listener） | `lifecycle.session_should_stop()` 或等价查询 |
   | `chatroom_tools.py:1214` 直接赋值 | 走 `lifecycle.mark_winding_down(..., end_session=True)`（新增语义参数替代 `flip_running`） |
   | `channels/telegram/__init__.py:544` | `engine.is_running`（已存在的 public property，`engine.py:331`，channels 层本该只走这个，不该碰 `_running`） |
   | `channels/telegram/commands/settings.py:297` | 同上，改用 `engine.is_running` |

4. **退役裸属性**：全部迁完后，`engine._running` 删除或降级为
   `@property`（内部转发到新状态源，读=兼容、写=raise 或 deprecation warning）。
   `round_lifecycle.py` 的 `flip_running` 参数一并删除。

**验证**：
- `pytest tests/ -q` 全绿，含新增的会话级状态回归测试
- `grep -rn "\._running\b" nanobot/ | grep -v "engine.py:.*is_running\|@property"` 结果
  仅剩 `engine.py` 内部实现细节，**channels/ 目录归零**（这是"channel 不碰
  orchestra 内部状态"边界的硬验收）

## Phase 2：`broadcast_round` 拆分

**前提**：Phase 1 完成，状态源已统一，拆分时不会把同一状态问题分散到多文件。

**目标**：把 `_run_one` 及其内嵌闭包提升为 `BroadcastOrchestrator` 的方法或
独立协作对象，让工具循环回调、监听器、超时判断可以脱离整轮广播单独测试。

1. 现状盘点：`BroadcastOrchestrator`（`broadcast.py:218`）目前只管
   `setup_tools_and_pools`（:259）。`_run_one`（:491）及其闭包
   `_on_tool_start`/`_on_tool_result`/`_inject_retry`/`_on_iter_usage`/`_badge`
   全部定义在 `broadcast_round` 函数体内，靠闭包捕获共享状态。
2. 逐个闭包提升为方法，参数化原来靠闭包捕获的变量（先从最独立的
   `_on_tool_start`/`_on_tool_result` 开始，风险最低；`_inject_retry` 和
   `_on_iter_usage` 涉及重试/用量统计，其次；`_run_one` 本体最后动）。
3. 监听器（`_user_listener`/`_join_listener`/`_watch_leader_end`/
   `_watch_no_leader_convergence`，:1517-1652）同理提升，评估是否可以独立
   成一个 `BroadcastListeners` 协作对象，减少 `broadcast_round` 函数体本身
   的行数。
4. 每提升一组方法，补对应单元测试（提升前只能靠集成测试覆盖，这是本阶段
   要解决的核心问题——提升后要能不跑整轮广播就测到这些分支）。

**验证**：
- `pytest tests/ -q` 全绿
- `broadcast_round` 函数体行数显著下降（记录提升前后对比，作为验收证据写进
  commit message）
- 新增的独立方法/对象有专属单元测试，不再只靠集成测试兜底

## Phase 3：channels/ 收敛（可与 Phase 1/2 并行）

**目标**：优先补测试盲区，再评估复用值不值得做——不要为了复用而重构没有
测试保护的代码。

1. **先补 `mochat.py`（946 行，零专属测试）的测试**，覆盖现有行为
   （AGENTS.md #1：没有回归测试守护，不许动实现代码）。
2. 视情况补 `discord.py`/`wecom.py`/`whatsapp.py`/`manager.py`/`registry.py`
   的专属测试——不要求一次性做完，按触碰频率排优先级。
3. 测试补齐后，评估把轮询循环 / 去重 / 媒体处理的公共部分提炼到 `base.py`
   或 `channels/utils/` 是否值得——如果收益不明确（参考
   `docs/phase4-findings.md` 4.2 对 Session/HistoryContext 的判断先例：
   "服务不同场景，不合并"），允许结论是"不做"，把结论和理由记录下来即可，
   不必强行合并。

**验证**：
- 新增测试全绿
- 若做了提炼：`pytest tests/ -q` 全绿，且原有 channel 特有行为测试不受影响

---

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Phase 1 遗漏某个 `_running` 读写点，行为悄悄改变 | 中 | 高 | 迁移前 grep 全量列出（本计划已列出已知 20+ 处作为基线），每改一处跑测试，最后 grep 归零验收 |
| Phase 1 触及生产网关正在跑的会话循环 | 中 | 高 | 遵循 AGENTS.md 网关重启规则：确认 agent idle 才重启验证；先在测试环境跑满全量测试 |
| Phase 2 提升闭包时遗漏某个隐式共享状态 | 中 | 中 | 逐个提升、逐个测试，不要一次性搬完 `_run_one`；先做最独立的 `_on_tool_start`/`_on_tool_result` 积累经验 |
| Phase 3 为了复用强行合并出现分歧的 channel 逻辑 | 低 | 中 | 参考 4.2 先例，允许"审计后判断不合并"作为合法结论 |
| 三个 phase 战线拉长，中途被打断 | 中 | 低 | 每个 phase 内部按"逐个迁移/提升/补测试"切成可独立提交的最小单元，AGENTS.md #6 |

## 不做（本轮范围外）

- providers/（4184 行）：审查后判断结构清晰、抽象合理，不列入本轮
- mods/skills 边界：代码层面确认无越界 import，`docs/SKILL_VS_MOD.md` 边界成立，不动
- Telegram 真正统一走 bus（上轮计划已判断"高风险，不推荐"）：本轮不重新评估
- channels/ 除 `mochat.py` 外的全量测试补齐：按需推进，不设一次性完成的硬指标

## 验证（DoD）

- [ ] Phase 1：`py_compile` 全过；`pytest tests/ -q` 全绿；
      `grep -rn "\._running\b" nanobot/channels/` 归零；
      `round_lifecycle.py` 的 `flip_running` 参数删除
- [ ] Phase 2：`pytest tests/ -q` 全绿；`_run_one`/`broadcast_round` 行数下降有
      commit 记录；提升出的方法有专属单元测试
- [ ] Phase 3：`mochat.py` 测试新增且绿；复用是否值得做的结论有记录（做或不做）
- [ ] 每个 phase 内每个逻辑单元单独 commit，message 写清 what + why + 证据
- [ ] 涉及线上：确认 agent idle → 重启网关 → 观察 gateway.log 首轮

## 参考文件

- `AGENTS.md` — 红线
- `docs/archive/plan-2026-09-07-history-refactor.md` — 上一轮已完成的历史模型重构（背景参考）
- `docs/phase4-findings.md` — 4.2 的"审计后判断不合并"先例，Phase 3 可参考
- `nanobot/groupchat/orchestra/engine.py:287-333` — `_running`/`is_running` 现状
- `nanobot/groupchat/orchestra/round_lifecycle.py` — 状态转换现状
- `nanobot/groupchat/orchestra/run_loop.py:107-183` — 会话主循环，Phase 1 核心改动点
- `nanobot/groupchat/orchestra/broadcast.py:377-1875` — `broadcast_round`，Phase 2 核心改动点
- `nanobot/channels/base.py`、`nanobot/channels/utils/` — Phase 3 复用评估起点
- `tests/test_round_lifecycle.py`/`tests/test_no_leader_convergence.py` — 现有轮次状态测试参考模式

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-09-07 | 创建本计划。基于历史模型重构（Phase A-E）完成后的独立架构审查：发现 `engine._running` 双语义比 `docs/phase4-findings.md` 描述的更严重（渗出到 channels 层）、`broadcast_round` 是 ~1500 行巨函数、channels/ 复用不足叠加测试盲区（`mochat.py` 零测试）。providers/ 和 mods/skills 边界审查后判断健康，不列入本轮。原历史模型重构 `plan.md` 归档至 `docs/archive/plan-2026-09-07-history-refactor.md`。 |
