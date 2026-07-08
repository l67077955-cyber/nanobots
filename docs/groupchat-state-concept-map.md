# 群聊引擎：状态/概念梳理与消歧图

> 2026-07-08 审计产物。逐字段盘点了 mailbox / engine / agent_runner+tracker+tool_loop /
> broadcast._run_one 的全部状态持有者与概念，按"歧义簇"归类。用作后续解耦重构的
> checklist。行号来自审计当日，可能随代码漂移——以字段名为准。
>
> 配套：`docs/groupchat-coupling-fix.md`（三端口重构计划）、`ports.py`（契约）。

## 核心结论

复杂度有两处是**承重**的（不能删，只能搬）：
1. **显示层** —— tracker 15 态状态机、`StreamingDisplay`（流式编辑/throttle/in-place finalize/4096 截断）、散在 `_run_one` 里的显示 body（token suffix、合成 finalize）。
2. **打断/取消并发协调** —— rank 校验 + 每轮 quota + 新鲜度 + busy 延期 + LLM race（2 分钟卡死的现场）。

其余都是**accidental complexity**（重复 / 死状态 / 派生却独立存），端口重构即消。mailbox 臃肿的根因：它一人背了**消息投递 + 并发态 + 打断协调**三份活，且每份都和别处重复一份。

---

## ① 三个"agent 状态"空间，互不对账

同一个"agent 现在干嘛"有三个独立来源，名字重叠但语义不同，无单一 owner 对账：

| 空间 | 值 | 写入者 | 性质 |
|---|---|---|---|
| `AgentRunner.state` | idle\|busy\|waiting\|interrupted\|done | 派生：读 mailbox 三 set + `interrupt_event` + `task.done()` | 运行态 |
| `tracker._states` | thinking\|searching\|fetching\|executing\|reading\|writing\|sending\|waiting\|interrupted\|done\|error\|cancelled\|blocked\|finishing | `tracker.set_state()` 各处（`broadcast.py` 多点） | **UI 显示态** |
| `result.finish_reason` | stop\|interrupted\|timeout\|error\|end_discussion\|max_iterations\|degenerate_repetition | tool_loop 各 checkpoint（`tool_loop.py:345/489/515/571/880/947/979`） | **cycle 产出** |

**陷阱**：`waiting`/`interrupted`/`done`/`error` 在三套里同名不同义。
- tracker `done`（orchestrator 异步设，滞后）≠ AgentRunner `done`（`task.done()`）≠ finish_reason `stop`。
- `chatroom_tools.py:1019` 的 `safe_states` 禁用守卫读 **tracker**（UI），从不读 AgentRunner（运行态权威）——UI 滞后时可能误判"可安全杀"。
- `finish_reason` 文档（`tool_loop.py:72`）只记了 stop/max_iterations/error，**interrupted/timeout/end_discussion/degenerate_repetition 未文档化**。

**interrupt 是瞬时的**（用户校正）：`AgentRunner.state` 的 `interrupted` 读 `interrupt_event.is_set()`，而该事件在 `acknowledge_interrupt()`（`agent_runner.py:109`）里被清——**清掉时 task 仍在 unwind**。所以 `interrupted` 是一瞬闪烁，不是驻留态。运行态实际上只有 **busy（tool_loop 在飞）/ idle（没在飞）/ done（终态）**；`waiting` 是 idle 的变体。→ `AgentRunner.state` 可简化为 busy/idle/done（Step 3 遗留）。

---

## ② 信号爆炸

### round 结束 ×3（汇于 `broadcast.py:1428-1429`）
- `engine._running`（`engine.py:295`）—— 外部 `/stop`，唯一外部可控。
- `leader_end_event.is_set()`（在 `BroadcastOrchestrator`，非 engine）—— leader end_discussion / crash。
- `mailbox.is_discussion_ended()`（`mailbox.py:890`）—— durable 全局锁。

后两个几乎总是一起 set（同一条 end_discussion 路径）→ **冗余**；`_running` 才是正交外部信号。三信号**不在一处重置**：`_running` 在 `_stop_group_loop`；`leader_end_event` 随 orchestrator 销毁；`_discussion_ended` 只在下轮 `mailbox.start_round` 清。→ 一轮可能以 `_running` 仍 True 结束（仅 leader_end/discussion_ended 触发），loop 继续下一轮。

### end-of-discussion ×4（各守不同出口）
1. `result.tools_used` 含 `end_discussion`（`broadcast.py:994`）—— 原始触发（"调了"≠"结束了"）。
2. `mailbox.is_discussion_ended()`（`mailbox.py:890`）—— **canonical 全局锁**（防重入，fan-out 源：同时 set `_all_waiting` + 所有 per-agent interrupt event）。
3. `leader_end_event` —— round 级唤醒 waiter。
4. `_leader_ended_discussion` 本地 bool（`broadcast.py:620`）—— 窄 carve-out：仅当 end_discussion 时 `_running` 已 False，让 leader 继续合成（`broadcast.py:854`）。

---

## ③ per-agent 注册表 / "我在干嘛" set

### 三套并行 per-agent 注册表（一个 round）
| 概念 | 位置 | 说明 |
|---|---|---|
| `_broadcast_tasks` | `engine.py:298` (dict[name,Task]) | per-agent asyncio task |
| `_runners` | `engine.py:302` (dict[name,AgentRunner]) | **新 canonical** handle（Step 3）|
| `mailbox._busy_agents` | `mailbox.py:337` (set) | 旧 in-tool_loop 集，`_runners` 欲取代 |

迁移未完成：`broadcast.py` 仍多处直读 `mailbox._busy_agents`。

### 三个"我在干嘛" set（mailbox，应折成一个 lifecycle enum）
| set | 语义 |
|---|---|
| `_active_agents`（`mailbox.py:313`）| 本轮未 done |
| `_busy_agents`（`mailbox.py:337`）| 在 tool_loop 内 |
| `_waiting`（`mailbox.py:307`）| 阻塞在 `wait()` |

一个 active agent 必在 `_busy_agents` ∪ `_waiting` 之一，但三 set 独立 mutate 可瞬时不一致。
- **跨对象重复**：`engine._active_agents`（list，`engine.py:279`）vs `mailbox._active_agents`（set）同概念两份，会 drift（`broadcast.py:1659` 直 mutate mailbox 的）。

---

## ④ 四个"message"结构
| 结构 | owner | 性质 |
|---|---|---|
| per-agent `messages` list | `_run_one`（`broadcast.py:609` 建）| **私有可变工作记忆**，按设计 desync 于 _history（prune/inject 只改它）|
| `engine._history` | `HistoryContext`/`ConversationContext` | 共享 canonical transcript（Step 1 已改只读 view，`engine.py:469`）|
| `mailbox._queues` | `MailboxHub`（`mailbox.py:300`）| 瞬时投递 FIFO（drain 即空）；同一条消息在 `_history` 和队列里各存一份（shared ref）|
| `ConversationContext.view_for` | 端口（`conversation_context.py`）| **期望态，尚未接入 broadcast**（L609 仍直接快照 _history）|

---

## ⑤ interrupt：事件与计数不对账

`_interrupt_events`（Event，`mailbox.py:329`）可被 `_try_interrupt`/`interrupt_busy_agents`/wait-nudge/`mark_discussion_ended` 多处 set；但 `_interrupt_counts`（每轮 cap 3，`mailbox.py:335`）**只** `_try_interrupt` 递增。→ 同一结构里两个"被打断几次"语义。
- `agent_runner.py:131` 无条件覆写 `_last_interrupt_sender`，可冲掉更高 rank 归因。
- `_last_interrupt_sender`（`mailbox.py:331`）**跨 round 不清**（`start_round` 不清它）→ 陈旧归因 bug。
- interrupt checkpoints（`tool_loop.py:341/452/710/886`）：只有 452 race 在飞 LLM；710/886 之间的 `asyncio.gather` 工具执行段**不轮询**——长 web_search/exec 批仍可能拖慢打断。`_CANCEL_UNWIND_TIMEOUT=5s`（`tool_loop.py:112`）只 bound 452，不 bound 工具执行；且留下 fire-and-forget 后台 task（潜在资源泄漏/乱序回调）。

---

## ⑥ 死 / 派生状态（accidental complexity，可直接清）

| 死状态 | 位置 | 真相 | 处置 |
|---|---|---|---|
| `_global_start` / `_global_timeout` | `mailbox.py:302-303` | "200s 硬上限"是幻觉——**从未被读**，round 无硬 cap | 删，或真正接进 wait() deadline |
| `_leader` vs `_leader_name` | `mailbox.py:324/310` | 同一事实两份，不同方法 set，可 drift；`broadcast.py:469` 还从 `_leader` 反推调 `set_leader_name`（冗余往返）| 折成一个字段一个 setter |
| `_ranks` vs `_base_ranks` | `mailbox.py:323/322` | 派生却独立存；`_try_interrupt` 用 `_ranks`、`_can_interrupt` 用 `_tier_rank`(_base_ranks)——一个打断决策两套 rank | 派生按需算 |
| `_all_waiting` | `mailbox.py:308` | 纯派生（`_waiting ≥ _active_agents`）却以 Event 存、三处命令式 set，历史漏过一处 | 改派生查询 |
| **`_leader_disabled_agent`** | `broadcast.py:621` | **写了从不读**——disable/restart 路径**并不**强制合成重试（`L1369` 守卫只查 `_leader_ended_discussion`+`not content`，注释撒谎，潜在 bug）| 删，或接进 L1369 条件 |
| `_timeout_recovery_count` | `broadcast.py:818` | 名为 count 实为 0/1 latch（成功/失败都清零，`L1059`/`L1086`）→ "repeated timeout" else 分支（`L1089`）**实际不可达** | 重命名/改语义，或删不可达分支 |
| `SpeakQueue` alias | `mailbox.py:276` | 向后兼容别名 = ConversationPool | 评估后删 |

---

## ⑦ "杀 mailbox" 执行清单（strangler 终局，对上三端口设计）

mailbox 三份活拆归端口，再删空壳：
1. `_busy_agents`/`_waiting`/`_active_agents` 三 set → 折成 `AgentRunner` 一个 lifecycle（**busy/idle/done**，见 ①）。
2. `_interrupt_events`/`_counts`/`_last_interrupt_sender` → 归 `AgentRunner`（cancel 信号已第一公民）。
3. `_queues`/`send`/`_history`(mailbox 的) → 归 `ConversationContext`（消息投递本就简单，几十行）。
4. 删死状态（⑥）。
5. mailbox 空壳 → 删；显示 body 从 `_run_one` 抽成独立 Display 模块（3c 本就要搬 body，顺带隔离显示复杂度）。

**前置依赖**：`wait()`/`send`/busy 跟踪现在埋在 `_run_one` body 里，不先理清 body（3b/3c）就没法干净地把它们搬到端口。

---

## 审计来源

4 个并行 reader agent 逐字段盘点（2026-07-08）：mailbox.py 全量、engine.py 全量、agent_runner+tracker(broadcast_status.py)+tool_loop、broadcast._run_one。完整结构化输出见会话 workflow `wf_075b2c44-709` 的 journal。
