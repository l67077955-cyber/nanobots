# Broadcast Live Prefix & Tier Truncation — 审计报告

**日期**: 2026-06-21  
**审计范围**: `broadcast_agent.py` + `message_converter.py`  
**审计人**: Harper (建构) + Kirk (审查)

---

## 1. `_apply_live_prefix` 触发条件

**文件**: `nanobot/groupchat/orchestra/broadcast_agent.py`

### 1.1 函数定义 (L264-282)

```python
def _apply_live_prefix(cycle_tail: list[dict], *, reason: str) -> None:
    nonlocal messages, _sys_msg_count, volatile_msg_idx, user_question, _prefix_history_len
    fresh_prefix, live_uq = _rebuild_prompt_prefix(engine, name, ...)
    messages[:] = fresh_prefix + cycle_tail
    _sys_msg_count = len(fresh_prefix)
    volatile_msg_idx = len(fresh_prefix) - 1
    user_question = live_uq
    _prefix_history_len = len(engine._history)
```

**行为**: 完全替换 `messages` 列表为 `fresh_prefix + cycle_tail`，更新所有追踪变量。

### 1.2 触发点 A — wait() 唤醒 (L960-1013)

```python
_history_grew = len(engine._history) > _prefix_history_len
_needs_rebuild = msg.sender == "用户" or _history_grew
if _needs_rebuild:
    _apply_live_prefix(cycle_tail, reason=f"wait reactivation by {msg.sender}")
else:
    messages.extend(cycle_tail)
```

**触发条件**: `msg.sender == "用户"` OR `engine._history` 增长。  
**证据**: `broadcast_agent.py:977-981`

### 1.3 触发点 B — interrupt 路径 (L733-736)

```python
if _sender_name == "用户" or len(engine._history) > _prefix_history_len:
    _apply_live_prefix(cycle_tail, reason=f"interrupt from {_sender_name}")
else:
    messages.extend(cycle_tail)
```

**触发条件**: 与 wait 路径一致 — 用户消息 OR history 增长。  
**证据**: `broadcast_agent.py:733-736`

### 1.4 触发点 C — 首轮初始化 (L316-322)

```python
if _initial_prefix:
    _apply_live_prefix([], reason="initial prefix")
```

**证据**: `broadcast_agent.py:316-322`

### ✅ 结论: 所有用户插话路径均正确触发 `_apply_live_prefix`

---

## 2. 分级消息裁剪 (Tier Priority)

**文件**: `nanobot/groupchat/history/message_converter.py`

### 2.1 `fit_messages_to_tier_budget` (L288-370)

**裁剪阶梯**:
1. **保留人类用户消息** — 完整保真度
2. **保留 agent 消息** — 从最新向前保留
3. **降级 in-message**: chatroom tools → age tools → strip tools
4. **丢弃可选消息** — 仍超预算时
5. **压缩** — 将丢弃/溢出压缩为单个 summary block

**证据**: `message_converter.py:296-305`

### 2.2 `degrade_content` (L130-155)

**降级级别**:
- Level 0: 原文不动
- Level 1: `strip_chatroom_tool_lines` — 仅移除协调工具行（chatroom_send/wait）
- Level 2: `age_tool_log` — 压缩工具预览为单行摘要
- Level 3: `strip_tool_log` — 移除整个工具调用块

**证据**: `message_converter.py:130-155`

### 2.3 `_is_human_user_llm` (L268-278)

**判定逻辑**: `sender` 为 `"User"/"user"/"用户"` 且 `role` 为 `"user"`。  
**证据**: `message_converter.py:268-278`

### ✅ 结论: Tier 优先级逻辑正确，用户消息永远不会被丢弃

---

## 3. 已识别缺口

### 3.1 ⚠️ 非 rebuild 分支仍用 `prune_conversation_tail_with_summary`

**位置**: `broadcast_agent.py:1015-1025`

```python
# ── Prune conversation tail (no history rebuild needed) ──
from nanobot.groupchat.history.tool_pruning import prune_conversation_tail_with_summary
```

**影响**: 当队友消息且 history 未增长时（`_needs_rebuild = False`），走 `messages.extend(cycle_tail)` 路径，然后 L1015 的 pre-tool_loop pruning 使用旧的 `prune_conversation_tail_with_summary`。

**风险评估**: **低风险**。此路径仅在队友间普通消息时触发，不涉及用户插话。裁剪的是 conversation tail（旧对话），不影响 prefix 准确性。但与 rebuild 路径使用的 `fit_messages_to_tier_budget` 不一致，长期应统一。

**证据**: `broadcast_agent.py:396-414` (pre-tool-loop pruning) vs `broadcast_agent.py:977-981` (rebuild path)

### 3.2 ℹ️ `_rebuild_prompt_prefix` 中 `latest_user_question` 截断为 300 字符

**位置**: `broadcast_agent.py:39`

```python
live_uq = latest_user_question(engine._history) or user_question
```

`latest_user_question` 在 `message_converter.py:280-284` 截断为 300 字符。

**影响**: 用户超长问题会被截断。这是有意设计（防止 prompt 膨胀），但应记录在案。

**证据**: `message_converter.py:284` (`content[:300]`)

### 3.3 ✅ 无其他缺口

两条核心链路（wait 唤醒 + interrupt）的触发条件一致且正确覆盖了「用户插话」场景。

---

## 4. 测试覆盖

**测试文件**: `tests/test_broadcast_live_prefix.py`  
**测试数量**: 23  
**结果**: **23 passed, 0 failed**

| 测试类 | 测试数 | 覆盖内容 |
|--------|--------|----------|
| TestRebuildPromptPrefix | 4 | _rebuild_prompt_prefix 核心逻辑 |
| TestBroadcastRuntimeInserts | 2 | leader/non-leader 运行时插入 |
| TestWaitReactivationLogic | 4 | wait() 唤醒时的 rebuild 决策 |
| TestInterruptPathLogic | 3 | interrupt 路径的 rebuild 决策 |
| TestLatestUserQuestion | 5 | 用户问题提取（含截断/跳过 summary） |
| TestDegradeContent | 3 | 内容降级 3 级 |
| TestFitMessagesToTierBudget | 2 | 分级裁剪（用户消息不丢弃、system[0] 强制保留） |
