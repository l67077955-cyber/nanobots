# nanobot-src 群聊历史模型重构计划

> 创建日期: 2026-09-07（覆盖上轮 Phase 1-4 记录）
> 状态: **Phase A–E 实现完成**（全量测试受 sandbox 只读文件系统阻断，见 DoD 备注）
> 范围: **只管群聊历史模型**（不动 engine/broadcast/callbacks 的耦合）
> 稳定性标准: **测试覆盖 + 接口契约 两者都要**——核心路径有回归测试守护，
>   模块间靠明确接口通信，外部代码不直接访问 `HistoryContext` 内部列表
> 红线遵循: AGENTS.md #1 先写测试再改实现、#2 修根源不堆护栏、#4 删死代码、#5 编辑前重读、#6 每逻辑单元 checkpoint 提交

## Context — 为什么要做这次修改

群聊系统存在两个用户报告的运行期 bug，调查后确认它们**不是四个孤立缺陷，而是同一个没定清楚的架构契约的两面**：

- **上下文遗忘**：`HistoryContext` 只有一条共享列表 `messages`，`max_messages=50`、
  `compression_keep_recent=6`、`compress_ratio=0.8`，40 条即触发压缩。`maybe_compress`
  把中段压成 ≤500 字摘要；摘要未启用/无 provider 时**直接丢弃中段**
  （`context.py:333-334` `self.messages = head + tail`）。每轮结束都跑
  （`run_loop.py:167`），3-5 agent 的群聊几轮就丢。
- **agent 间消息收不到**：轮内传递**只走 mailbox**（共享历史在轮开始时快照一次、
  轮中不重读，`broadcast.py:513 → prompt_builder.py:723`）。而 `ChatroomSendTool`
  **只调 `mailbox.send()`，从不写共享历史**（`chatroom_tools.py:761`）。一旦实时打断
  失败——默认 `rank=pawn` 同级不能互相打断（`mailbox.py:486` `s_rank > t_rank`），
  或 `end_discussion` 取消任务时 agent 还在 cycle、到不了 auto-wait——消息就永久丢，
  且不在任何历史里。
- **IngressRouter 半统一**（上轮 plan.md 1.1 标"已修复"是言过其实）：Telegram 主
  通道在有活跃 agent 时仍直接调 `engine.inject()` 并 return（`message_handler.py:159`），
  `IngressRouter` 在生产里收不到 Telegram 消息；`deliver_user_message()` 是 `inject()`
  的逐行克隆，额外**丢弃 `media`/`metadata`**（`engine.py:864-865`），0 agent 时静默丢消息。

**根因**：系统有两套互相矛盾的历史模型，没有不变量——持久的那套（`engine._history`）
会忘会串台（A 的输出 C 也看得到）；范围对的那套（mailbox，A→B 只有 B 的队列有）
不持久。用户要求的语义正是把两者合并成一套：

> **A 发给 B 的消息，C 看不到，只有 AB 可见**（mailbox 的可见范围）
> **+ 跨轮留存**（`engine._history` 的持久性）
> **+ 压缩对每个 agent 各自的可见子集分别做**（不串台）

### 用户已拍板的设计决策
- 默认可见性：无 `targets` 的消息 = 全员可见（`["All"]`）
- 用户消息：全员可见
- 压缩触发粒度：**每个 agent 独立阈值**，各自到阈值各自压缩

---

## 目标架构：单持久日志 + per-agent 持久视图

```
                    ┌─────────────────────────────────────┐
  所有消息写入 ───► │  持久日志 (append-only, 带 targets)  │
  _add_message()    │  [{sender, content, targets}, ...]   │
  chatroom_send     └──────────────┬──────────────────────┘
                                   │ 按 targets 可见性投影
                    ┌──────────────┴──────────────┐
                    ▼                              ▼
          ┌────────────────┐              ┌────────────────┐
          │ Agent A 视图    │              │ Agent B 视图    │  ... (N 个)
          │ (持久压缩态)    │              │ (持久压缩态)    │
          └────────┬───────┘              └────────┬───────┘
                   │ 各自阈值各自压缩              │
                   ▼                               ▼
          build_agent_prompt(A)           build_agent_prompt(B)
          (读 A 的视图，不重压)            (读 B 的视图，不重压)
```

**不变量**（改动必须保持）：
1. 一条消息进日志后，**只出现在其 `targets` 命中的 agent 视图里**；A→B(`targets=[B]`)
   不进 C 的视图。
2. `["All"]` 的消息进**所有** agent 视图（含用户/系统消息——默认全员可见）。
3. 每个 agent 视图独立维护自己的压缩态；压缩只动该视图，不碰其他 agent 的视图，
   **不破坏跨 agent 隐私**（A→B 段在 A 视图压一次、在 B 视图再压一次，算力翻倍
   但换正确隐私——用户已确认接受）。
4. mailbox 降级为**实时通知层**：消息已在持久日志里，打断失败不再丢——agent 下轮
   建 prompt 时按可见性过滤就会看到。
5. **接口契约**：外部代码（engine/broadcast/run_loop/chatroom_tools/tool_loop）
   只通过 `HistoryContext` 的稳定 public 方法访问历史，**永远不直接读/写内部
   `messages` 列表**。消灭当前三类"无契约"访问（见下）。

### 接口契约 — `HistoryContext` 稳定 public 面

> 这是"低耦合"的具体落地。当前外部代码直接戳 `engine._history`（34 处/8 文件），
> 包括读列表元素、读私有属性、原地改写列表——这些都是无契约访问，改内部实现
> 就静默崩调用方。重构后 `HistoryContext` 只暴露以下操作，外部全部走它：

```python
class HistoryContext:
    # ── 写入 ──
    def add_message(self, sender: str, content: str,
                    targets: list[str] | None = None) -> None:
        """追加一条消息。targets=None → 全员可见。唯一写入入口。"""

    # ── 读取（per-agent 视图）──
    def view_for(self, agent_name: str) -> list[dict]:
        """返回该 agent 可见的消息子集（持久视图的副本，外部不可改）。"""

    def view_for_raw(self, agent_name: str) -> list[dict]:
        """同 view_for 但未经压缩——供调试/quote_message 等需原文的场景。"""

    # ── 查询（替代直接读列表元素）──
    def last_sender(self) -> str | None:
        """最后一条消息的 sender（替代 engine._history[-1]['sender']）。"""

    def has_system_message(self) -> bool:
        """是否已有系统消息（替代 any(m['sender']=='系统' for m in engine._history)）。"""

    def is_empty(self) -> bool:
        """替代 `not engine._history`。"""

    def all_messages(self) -> list[dict]:
        """完整日志的副本——仅 generate_summary 等需全量场景用。"""

    # ── 压缩 ──
    async def compress_for(self, agent_name: str) -> None:
        """压缩指定 agent 的视图（per-agent 独立阈值）。"""

    async def compress_all(self) -> None:
        """对所有 active agent 各压一次（替代 _maybe_compress_history）。"""

    # ── 维护 ──
    def clear(self) -> None:
        """清空日志和所有视图。"""

    def clear_agent_view(self, agent_name: str, keep_last: int = 0) -> int:
        """清理某 agent 视图（替代 ClearContextTool 的 _history[:] = new_history）。
        返回清理条数。"""

    def format(self) -> str:
        """格式化全量日志为可读字符串（调试用）。"""
```

**禁止的外部访问**（重构后必须消除，grep 验证归零）：
- `engine._history[-1]["sender"]` → `engine.history.last_sender()`
- `for m in engine._history` / `reversed(engine._history)` → `engine.history.all_messages()` 或 `view_for(name)`
- `engine._history._provider` → 由 `HistoryContext` 内部持有，外部不碰
- `engine._history[:] = new_history` → `engine.history.clear_agent_view(name)`
- `self._history = self.history.messages` 同步仪式（5 处）→ 删除，shim 退役

---

## 现有代码关键事实（已逐行核实）

| 事实 | 位置 | 含义 |
|------|------|------|
| `HistoryContext.messages` 是单条共享列表 | `context.py:50` | per-agent 视图**不存在**，要新建 |
| `engine._history` 是 `history.messages` 的别名 shim | `engine.py:287,427,434,983,1015` | 34 处引用、8 文件，要迁移 |
| 消息只有 `sender`/`content`，无 `targets` | `context.py:104` | 要加字段 |
| `history_to_messages` 按 `sender` 过滤（`allowed = {"用户","系统"} \| relevant_agents`） | `message_converter.py:84` | 要改成按 `targets` 可见性过滤 |
| broadcast 建 prompt 传 `relevant_agents=None`（不过滤，全员看全部） | `broadcast.py:515` | 要换成传该 agent 的视图 |
| `ChatroomSendTool` 只 `mailbox.send()`，不写历史 | `chatroom_tools.py:761` | 要加写日志 |
| `MailboxHub.start_round()` 清空队列和历史 | `mailbox.py:411-432` | 持久化后可清空（消息已在日志） |
| `maybe_compress` 对共享列表整体压 | `context.py:160-340` | 要改成 per-agent |
| direct-chat 模式也用 `_add_message` + `_maybe_compress_history` | `engine.py:1195-1200` | 单 agent 时视图退化为"自己=全部"，要兼容 |
| 测试范式：`_FakeEngine` 真对象+微小假件，`_add_message(sender, content)` | `tests/test_user_ingress.py:33` | 加 `targets` 参数时假件要跟着改 |

**调用链插入点**（已确认）：
```
broadcast.py:513  engine._build_agent_prompt(history=self._history, relevant_agents=None)
  → engine.py:1055  PromptBuilder.build_agent_prompt(history=self._history, ...)
    → prompt_builder.py:723  history_to_messages(history, agent_name, relevant_agents=...)
      → message_converter.py:84  按 sender 过滤
```
改造点：`engine.py:1055` 把 `history=self._history` 换成 `history=self.history.view_for(agent_name)`。

---

## 执行计划（分步提交，每步先写回归测试）

> 每步：① 写/改回归测试钉住行为 → ② 改实现 → ③ `py_compile` + `pytest tests/ -q` 全绿 → ④ checkpoint 提交
> 提交 message 写清 what + why + 证据（引用测试/行号）

### Phase A：数据结构加 `targets` 字段（无行为变更）

**目标**：消息结构支持可见性，但默认全员可见 = 行为不变。

1. **测试**（新建 `tests/test_history_targets.py`）：
   - `add_message(sender, content)` 不传 targets → 默认 `targets == ["All"]`
   - `add_message(sender, content, targets=["B"])` → `targets == ["B"]`
   - 旧调用点（`_add_message("用户", msg)` 等）不改也通过（默认全员）

2. **实现**：
   - `HistoryContext.add_message(self, sender, content, targets=None)`：缺省
     `targets = ["All"]`；`self.messages.append({"sender","content","targets"})`
   - `engine._add_message(self, sender, content, targets=None)` 透传
   - `_state.save_message` 多存一个 `targets` 字段（向后兼容：旧日志无 targets
     读取时补 `["All"]`）

3. **迁移现有调用点**（逐个改，不强制传 targets，默认即全员）：
   - `run_loop.py:108` `_add_message("系统", ...)` → 默认 All
   - `user_ingress.py:55,142` `_add_message("用户", ...)` → 默认 All
   - `engine.py:1195,1198` direct-chat → 默认 All
   - `broadcast.py:901,927,987,1018,1121` agent 最终输出 → 默认 All
   - 这些**全部不改语义**（原来就全员可见），只是字段补全

**验证**：`pytest tests/ -q` 全绿（含旧测试，因默认全员 = 原行为）。

### Phase B：per-agent 视图结构（只读路径，不压缩）

**目标**：每个 agent 能取到自己该看到的子集，但此时子集是**临时算的**（从日志
按 targets 过滤），压缩仍走旧的共享 `maybe_compress`。本步只验证可见性正确。

1. **测试**（`tests/test_history_view.py`）：
   - A→B(`targets=["B"]`) → `view_for("A")` 含、`view_for("B")` 含、`view_for("C")` **不含**
   - 广播(`targets=["All"]`) → 所有 agent 的视图都含
   - 用户消息(默认 All) → 所有视图含
   - 视图是**投影**（改视图不污染日志，反之日志新增后视图重算能拿到新消息）

2. **实现**：
   - `HistoryContext.view_for(agent_name) -> list[dict]`：返回 `[m for m in self.messages
     if "All" in m["targets"] or agent_name in m["targets"] or m["sender"] == agent_name]`
     （发送者总能看到自己发的）
   - `engine._build_agent_prompt`（`engine.py:1055`）：`history=self.history.view_for(agent_name)`
     替换 `history=self._history`
   - `build_agent_prompt` 的 `relevant_agents` 参数**保留但置 None**（视图已过滤，
     再按 sender 过滤是冗余兜底；置 None 避免双重过滤误删）
   - `history_to_messages` 的 sender 过滤逻辑**不动**（兜底保留，向后兼容 direct-chat
     等仍传共享列表的场景）

3. **`_history` shim 处理**：本步**不删** shim。`engine._history` 仍指向 `history.messages`
   （日志），34 处读引用继续工作；只是 prompt 路径改走 `view_for`。删 shim 留到 Phase E。

**验证**：`pytest tests/ -q`；新增可见性测试全绿；旧测试因默认全员仍绿。

### Phase C：`chatroom_send` 写入持久日志

**目标**：agent 间讨论跨轮留存，不再只靠临时 mailbox。

1. **测试**（`tests/test_chatroom_send_persists.py`）：
   - agent A 调 `chatroom_send(to="B", msg)` → `engine._history` 出现一条
     `targets=["B"]` 的记录；`view_for("C")` 不含
   - 跨轮：`mailbox.start_round()` 清空队列后，`view_for("B")` 仍含该条（持久）
   - `chatroom_send(to="All")` → `targets=["All"]`，所有视图含

2. **实现**：
   - `ChatroomSendTool.execute`（`chatroom_tools.py:761`）在 `mailbox.send()` 后，
     调 `engine._add_message(self._agent_name, message, targets=actual_recipients)`
     - `to="All"` → `targets=["All"]`
     - `to=["B","C"]` → `targets=["B","C"]`
     - `to="B"` → `targets=["B"]`
   - `ChatroomSendTool` 需要 `engine` 引用（目前只有 `mailbox`）。在 `BroadcastOrchestrator
     .setup_tools_and_pools`（`broadcast.py:260`）构造 `ChatroomSendTool` 时注入
     `engine=self.engine`

3. **mailbox 角色**：仍保留 `mailbox.send` 做实时通知 + 打断。但消息已在日志里，
   打断失败不再"丢"——agent 下轮建 prompt 会从视图看到。

**验证**：`pytest tests/ -q`；跨轮留存测试绿。

### Phase D：压缩改为 per-agent（核心，风险最高）

**目标**：每个 agent 视图各自到阈值各自压缩，结果**存回该视图**（持久，不重压）。
**这是真正解决"遗忘"的一步**——压缩结果稳定可复用。

1. **测试**（`tests/test_per_agent_compress.py`）：
   - A 视图到阈值压缩 → A 视图变短、**B 视图不受影响**
   - A→B 段：在 A 视图被压成摘要、在 B 视图也各自压（独立）
   - 压缩摘要只在该 agent 视图内，不泄露给 C（A→B 的摘要**不进 C 视图**）
   - 压缩结果持久：第二次 `view_for("A")` 不再调 LLM（用存好的）
   - direct-chat 单 agent：视图=全部，压缩等价旧行为（兼容）

2. **实现**（核心数据结构改造）：
   - 新增 `HistoryContext._views: dict[str, list[dict]]` —— per-agent 持久视图
   - `add_message(sender, content, targets)`：**先写日志**（`self.messages`），
     再 append 到 `targets` 命中的每个 agent 的 `_views[name]`（`All` → 所有 active agent）
   - `view_for(name)`：直接返回 `self._views.get(name, [])`（不再临时算）
   - `maybe_compress_per_agent(name)`：对该 agent 的 `_views[name]` 跑压缩逻辑
     （复用现有 `maybe_compress` 的 head/tail/摘要算法，但作用域是 `_views[name]`）
   - `_maybe_compress_history`（`engine.py:1012`）：遍历 active agents 各自压一遍，
     各自独立阈值
   - 触发时机：`run_loop.py:167` 每轮结束、`engine.py:1200` direct-chat 每周期——
     改成对每个 active agent 调一次

3. **隐私保证**：压缩只读 `_views[name]`，摘要写回 `_views[name]`，**不跨视图**。
   A→B 段在 `_views["A"]` 和 `_views["B"]` 各自独立存在、独立压缩，C 的
   `_views["C"]` 从未含此段 → 摘要不泄露。

4. **兜底分支修正**：旧 `context.py:333-334` 的"摘要未启用就丢中段"——per-agent
   版本改成**像空摘要那样保留中段**（`return` 而非 `head+tail`），靠 `add_message`
   的 max_messages 兜底。先写测试钉"禁用摘要时中段不丢"。

**验证**：`pytest tests/ -q`；per-agent 隔离测试 + 持久测试绿；`test_compression_*_snapshot.py`
旧快照测试需更新（压缩模型变了，更新快照并记录原因）。

### Phase E：接口契约落地 + shim 退役 + IngressRouter 清理

**目标**：把外部对 `engine._history` 的 34 处直接访问全部迁到 `HistoryContext`
契约方法（上节"接口契约"），`_history` shim 退役，删死代码，阈值止血。
**这是"低耦合"标准的具体验收步**——grep 验证外部不再碰内部列表。

1. **外部访问点 → 契约方法**（逐个迁移，每改一处跑测试）：

   | 当前访问 | 出现处 | 迁到 |
   |----------|--------|------|
   | `engine._history[-1]["sender"]` | `engine.py:1030,1036` | `engine.history.last_sender()` |
   | `any(m["sender"]=="系统" for m in engine._history)` | `run_loop.py:107` | `engine.history.has_system_message()` |
   | `not engine._history` / `if not engine._history` | `run_loop.py:32,52` | `engine.history.is_empty()` |
   | `list(engine._history)` / `for m in engine._history` | `run_loop.py:35,107` `broadcast.py:446` | `engine.history.all_messages()`（摘要场景）或 `view_for(name)` |
   | `engine._history._provider` | `run_loop.py:52` | `HistoryContext` 内部持有，外部通过 `compress_all()` 触发，不直接拿 provider |
   | `self._engine._history[:] = new_history` | `chatroom_tools.py:1281`（ClearContextTool） | `engine.history.clear_agent_view(name, keep_last)` |
   | `history=self._history`（建 prompt） | `engine.py:1055` | `history=self.history.view_for(agent_name)`（Phase B 已做） |
   | `self._history = self.history.messages` 同步仪式 | `engine.py:287,427,434,983,1015` | **删除**——shim 退役 |

2. **`_history` shim 退役**：所有引用迁完后，删 `engine._history` 属性及 5 处同步行。
   留一个 `@property _history` 短期兼容层（raise 或 deprecation warning）可作
   可选项，但目标是真的删掉。

3. **IngressRouter 半统一**（二选一，**推荐删死代码**）：
   - 删 `deliver_user_message()`（`engine.py:842-871`）和 `inject()` 的重复——
     保留 `inject()` 作为唯一入口（Telegram 一直用它），`IngressRouter` 调 `inject()`
     而非 `deliver_user_message()`
   - 或：真统一 Telegram 走 bus（修 `deliver_user_message` 保 media/metadata + 0-agent
     处理，`message_handler.py:159` 改 `publish_inbound`）——风险高，不推荐本轮做
   - 修 `plan.md` 上轮 1.1 状态：从"已修复"改"未完成（本轮清理）"

4. **阈值调整**（止血，可与 Phase D 同提交）：
   - `history_settings.py:59-65`：`max_messages` 50→200、`compression_keep_recent`
     6→20。先写"200 条仍存活"回归测试。

**验证**：
- `pytest tests/ -q` 全绿
- `grep -rn "engine\._history\b\|\.history\.messages" nanobot/ | grep -v "context.py"` 
  **归零**——外部不再碰内部列表（这是"接口契约"的硬验收）
- `grep -rn "\._history\b" nanobot/groupchat/orchestra/engine.py` 仅剩兼容层或归零

---

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Phase D 视图一致性 bug（消息漏进/多进某视图） | 中 | 高 | Phase B 先用临时投影验证可见性逻辑，Phase D 再持久化；隔离测试钉死 |
| 压缩 per-agent 算力翻 N 倍 | 高 | 中 | 用户已确认接受；可加"仅 active agent 才压缩"优化 |
| `_history` shim 34 处迁移遗漏 | 中 | 中 | 每改一处跑全测试；Phase E 最后 grep 归零验证 |
| direct-chat 模式回归 | 低 | 高 | Phase D 兼容测试（单 agent 视图=全部）；`test_session_manager_history.py` 守护 |
| 压缩快照测试模型变了 | 高 | 低 | `test_compression_*_snapshot.py` 更新快照，commit message 记原因 |
| 线上网关在跑 | — | 高 | AGENTS.md：agent idle 才重启；改动不涉及 channel 层，降低风险 |

## 不做（本轮范围外）

- Telegram 真统一到 bus（D1 的"高风险"分支）——本轮只删死代码 + 修虚标
- mailbox 完全删除——降级为通知层，仍保留实时打断能力
- 历史可重载快照（进程重启恢复）——`save_message` 仍只追加日志，本轮不做快照恢复

## 验证（DoD）

- [x] `py_compile` 全过（本轮改动文件）
- [x] `pytest tests/ -q` 全绿：`695 passed, 32 deselected`（受限沙箱内运行时，gateway
      路径测试会因无法写入 `/root/.nanobot` 报只读；升级权限复验已通过）。
- [x] **接口契约验收**：`grep -rn "engine\._history\b\|\.history\.messages" nanobot/ | grep -v "context.py"`
      归零——外部不再碰内部列表（"低耦合"硬指标）
- [x] 新增测试覆盖（"高稳定"硬指标）：可见性隔离（A→B C 看不到）、跨轮留存、
      per-agent 压缩隔离、压缩持久不重压、禁用摘要不丢中段、契约方法行为
      （last_sender/has_system_message/is_empty/all_messages 返回副本不可改）
- [x] commit message 每步写清 what + why + 证据
- [ ] 涉及线上：确认 agent idle → 重启网关 → 观察 gateway.log 首轮

## 参考文件

- `AGENTS.md` — 红线（#1 先测试 #2 修根源 #4 删死代码 #5 重读 #6 checkpoint）
- `nanobot/groupchat/history/context.py` — `HistoryContext`（核心改造对象）
- `nanobot/groupchat/history/message_converter.py:history_to_messages` — 过滤逻辑
- `nanobot/groupchat/orchestra/engine.py:_build_agent_prompt` — 插入点
- `nanobot/groupchat/orchestra/tools/chatroom_tools.py:ChatroomSendTool` — 写日志
- `tests/test_user_ingress.py` — 测试范式（真对象+微小假件）
- `tests/test_compression_*_snapshot.py` — 快照测试（Phase D 要更新）

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-09-07 | 覆盖上轮 Phase 1-4 记录，重写为"单持久日志 + per-agent 视图"历史模型重构计划。基于遗忘/收不到两 bug 的根因调查（两套矛盾历史模型无不变量）+ 用户拍板的可见性语义（A→B C 看不到 + 跨轮留存 + per-agent 分别压缩）|
| 2026-09-07 | Phase A–D 已由 `6f0b5a57`、`01e3ed42`、`4455ba39`、`5319ce6a` 完成；Phase E 由 `47367677`、`2f406173` 完成：退役 `_history` shim 与重复 ingress、落地 HistoryContext 契约、提高默认历史窗口至 200/20，并删除摘要不可用时误压缩原始日志的遗留块。|
