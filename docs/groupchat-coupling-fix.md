# 群聊引擎：2 分钟卡死诊断 + 耦合控制方案

> 本文是 2026-07-08 一次排查对话的压缩上下文，作为后续修复与重构的稳定参照。

## 1. 故障现象

用户在群聊中发送 `cc的tarik还是tariq` 后，群聊卡死约 2 分钟，`/stop` 后才恢复。
日志：`/root/.nanobot/logs/gateway.log`。

## 2. 精确时间线（已从日志还原）

| 时刻 | 事件 |
|---|---|
| 11:58:44.236 | Kirk 调用 `wait(from_agent="Harper", timeout=45)` |
| 11:58:58.065–072 | 用户消息到达，`_user_listener` **立即**处理，对 Kirk+Harper 都 set 了 interrupt_event |
| 11:59:29 → 12:00:27 | Kirk 的 `wait()` 连续 12 次 `+5s` 延期（busy repliers: {Harper}） |
| 12:00:32.486 | Kirk `wait()` 放弃；12:00:34.059 返回，实际 **108.25s** |
| 12:00:34.159 | Harper 才打出 `interrupt detected DURING LLM call (iter 2)` |
| 12:00:34.33 | `/stop` 取消所有 broadcast task |

**结论**：引擎在 11:58:58 就收到用户消息并设置了打断——卡死的不是“消息没进来”，而是**打断信号无法终止两个正在阻塞的 agent**。Telegram 里“── User ──”晚到 12:00 只是发送队列被拖累的症状。

## 3. 根因

### 根因 1（主因）：`mailbox.wait()` 无视 interrupt_event

`nanobot/groupchat/orchestra/mailbox.py:640-770` 的 `wait()`：5s 轮询 mailbox 队列，到期后只要还有 busy 队友就 `+5s` 延期（最多 12 次），**整个循环从不检查 interrupt_event**。

`interrupt_event` 只在 `tool_loop` 的 LLM 调用前后 / 工具批次前后检查（`tool_loop.py:335/698/874`）。`wait()` 是一个**工具调用**，正卡在 `tool_loop.py:717` 的 `asyncio.gather`（工具执行阶段，无打断检查点）里。Kirk 被 `wait()` 关了 108 秒。

### 根因 2（次因）：`tool_loop` 的 LLM 打断竞速 `await llm_task` 无界

`tool_loop.py:466-478`：

```python
if intr_task in done:
    llm_task.cancel()
    try:
        await llm_task          # ← 阻塞 ~96s
    except (CancelledError, Exception):
        pass
    logger.info("⚡ interrupt detected DURING LLM call")  # 在 await 之后
```

日志 `interrupt detected DURING LLM call` 在 `await llm_task` **之后**才打（11:58:58 设打断 → 12:00:34 才打），说明 `cancel()` 后 `await llm_task` 阻塞 ~96s。原因：provider 每次 `acompletion` `timeout=20`（`litellm_provider.py:785`），`chat_with_retry` 重试 `(1,2,4)` ≈ 4 次 ≈ 87–96s；`chat()` 把异常吞成 `finish_reason="error"` 再被当 transient 重试；litellm 部分 provider 在 thread executor 跑同步 HTTP，`task.cancel()` 无法即时中断线程。Harper 因此一直留在 `_busy_agents`，使 Kirk 的 `wait()` 持续延期。

**两 bug 互锁** = 用户看到的 ~2 分钟无响应。

## 4. 架构诊断（耦合为何不可遏制）

- **上下文**：`HistoryContext`（`history/context.py`）已拥有 sender-based 共享日志；但 **role-based、per-agent 的 LLM 消息 list** 是独立结构，在 `_run_one` 的 `while True` 里贯穿 `tool_loop` **就地 mutate**（volatile 消息改写、pre-tool_loop 剪枝、forget excise、interrupt 注入）——这是真正“散落”的上下文。
- **顺序**：`_run_one` 是 `while True` + 约 7 个 `continue` 重入分支，不是 stack。发言顺序靠外部 `speak_order` + per-agent cycle + interrupt 重入拼出来。
- **并发**：agent 活跃态散落在 `mailbox._busy_agents` / `_interrupt_events` / `_interrupt_counts` / `_waiting`、`ConversationPool` 信号量、`engine._broadcast_tasks`——没有单一 `AgentRunner` 拥有状态机。interrupt 是一个独立 `asyncio.Event` 侧信道，阻塞操作（`wait`、在飞 LLM）必须**协作式轮询**它，但它们不轮询——这正是卡死根因。

## 5. 目标：三端口（seam）+ Strangler Fig 迁移

不推倒重来。立 3 个窄端口（`nanobot/groupchat/orchestra/ports.py`，Protocol），未来功能只许依赖端口；旧代码作为适配器活在后面。

```python
class ConversationContext(Protocol):
    def add(self, role, content, **meta) -> None: ...
    def forget(self, tool_call_ids: set[str]) -> None: ...
    def view_for(self, agent: str) -> ContextView: ...   # 唯一 LLM 入参构造口
    async def compress(self) -> None: ...

class AgentRunner(Protocol):
    name: str
    state: AgentState               # idle/busy/waiting/interrupted/done 唯一来源
    def cancel(self, reason: str) -> None: ...   # 取消信号第一公民
    async def run_turn(self, frame: TurnFrame) -> None: ...

class TurnStack(Protocol):
    def push_turn(self, agent: str, trigger: Trigger) -> None: ...
    def interject(self, user_msg: str) -> None: ...
    async def next_frame(self) -> TurnFrame: ...
```

**关键不变量**：`AgentRunner.cancel()` 是第一公民信号，任何阻塞操作（`wait`、在飞 LLM、未来的任何 await）都必须 race 它。这条立住，2 分钟卡死从结构上不再可能。

### 迁移顺序（风险递增，每步可独立停手）

| 步 | 做什么 | 风险 |
|---|---|---|
| **0** | 最小修复：`wait()` 接 interrupt_event；`tool_loop` 的 `await llm_task` 加硬超时 | 极低 |
| **0.5** | `AgentRunner` facade（不搬状态，委托 mailbox/engine） | 低 |
| **1** | `ConversationContext` 吸收 `view_for(agent)`；`tool_loop` 改吃 view 不吃裸 list | 中 |
| **2** | `TurnStack` 接管 speak_order + cycle-loop；7 个 `continue` 统一为 `push_followup` | 高（最后做） |

### 遏制未来耦合的两条硬约束

1. 依赖方向单向：`ports.py` 不 import 任何实现；新功能不能用端口表达 → 扩展端口，不许绕过直捣 mailbox/engine。
2. CI 守卫：把 `mailbox._busy_agents`、`engine._broadcast_tasks`、`tool_loop` 的 `messages` 形参列为禁止新代码直接引用（ruff TID251 / `__all__`）。

## 6. 已落地（Step 0 + Step 0.5）

### Step 0：最小修复（堵住两个根因）

1. `mailbox.MailboxHub.wait()`：每次轮询前检查 `self.get_interrupt_event(agent_name)`；已 set 则立即 `return None`，跳过延期。→ 堵根因 1（Kirk 108s）。
2. `tool_loop`：`llm_task.cancel()` 后 `await asyncio.wait_for(llm_task, timeout=_CANCEL_UNWIND_TIMEOUT)`（5s 模块常量），不再无界 await。→ 堵根因 2（Harper 96s）。

### Step 0.5：AgentRunner facade（端口种子，已落地）

- `nanobot/groupchat/orchestra/ports.py` —— 三端口 Protocol（`AgentRunner` / `ConversationContext` / `TurnStack`），团队契约。
- `nanobot/groupchat/orchestra/agent_runner.py` —— `AgentRunner` 具体类（委托 facade，不搬状态）：`interrupt_event` / `task` / `is_busy` / `state` / `force_interrupt` / `request_interrupt` / `cancel`。
- `engine.py`：`self._runners` 注册表 + `runner(name)` 访问器 + `runners` 属性；`_stop_group_loop` 清理。
- `broadcast.py _run_one`：每 agent 创建 runner 并注册；`_interrupt_event` 改从 `_runner.interrupt_event` 取（同一事件对象，**零行为变更**）。
- 测试：`tests/test_agent_runner.py`（5 例）。全量 567 passed，11 个预存失败与本改动无关。

**契约已立**：未来加 agent 运行时功能，调 `engine.runner(name).interrupt(...)` / `.cancel(...)`，不许直连 `mailbox._busy_agents` / `_interrupt_events`。`wait()` 与 `tool_loop` 的取消信号现以 runner 为规范来源。下一步（Step 1）把 `view_for(agent)` 收口到 `ConversationContext`，让 `tool_loop` 不再吃裸 list。

## 7. 本方案不解决（诚实边界）

- provider 层重试/超时策略（`chat()` 吞异常成 error-resp 被 retry 放大）。
- outbound 发送队列背压（telegram 进度编辑拖慢“── User ──”）。
- leader/pool/credit 经济学（会搬家，语义不简化）。

端口把它们与核心调度解耦后，可各自慢慢治。
