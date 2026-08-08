# broadcast.py:5/27 稳定版 vs 当前版 逻辑对比

- 对比对象:`v-stable-20260527`(456ae97ce) vs 当前 `stable-20260803-fixes` HEAD(`fe958da14`)
- 当前版行数:1744(比 5/27 的 1855 少 111 行)
- 生成时间:2026-08-06
- 备注:当前 HEAD 已包含 no-leader 收敛哨兵修复(broadcast.py L1612-1664)及其测试 `tests/test_no_leader_convergence.py`

---

## 一、讨论结束 / 无回复处理机制(核心差异)

| 维度 | 5/27 稳定版 | 当前版 |
|---|---|---|
| "没人回复"处理 | 每 agent `MAX_CONSECUTIVE_WAITS=3`:连续 3 次空 wait 超时→退出(reason="wait timeout x3") | 移除该计数,改为全局 no-leader 收敛哨兵:全员 wait→静默 15s→历史长度稳定→判收敛结束 |
| 结束判定 | 检查 `not running OR (mailbox.is_discussion_ended())` | 只查 `not engine._running`;`is_discussion_ended()` 概念被移除,改用 `leader_end_event` |
| leader 结束后优雅退出 | 15s GRACE_PERIOD:通知其他 agent 收尾→等待自然完成→再强杀 straggler | 移除宽限期,直接立刻 `cancel()` 非 leader |
| 合成校验例外 | `finish_reason=="end_discussion"` 时跳过长度校验,干净退出 | 移除该特例,一律强制校验合成长度 |
| 全局超时 | 超时后继续 | 超时置 `engine._running=False`,防止 run_loop 再起一轮 |

**核心结论:** 5/27 用"单 agent 等 3 次就撒手"的粗粒度防死锁;当前版改成专用收敛哨兵。把"讨论结束"从 leader 中心化彻底解耦。这正是 Harper/Lucas 无限互发问题的解药。

---

## 二、rank 体系 / 工具注册 ⚠️(当前环境有兼容隐患)

- **rank 命名体系**
  - 5/27:现代四级 `basic/standard/advanced/expert`,启动时**自动迁移**旧棋名(pawn→basic 等)并**重写磁盘 config**。
  - 当前版:**绕过迁移、直接用棋名五级** `pawn/knight/bishop/queen/king`(`_RANK_ORDER` L536,L540),leader=rank+1,用于工具调用隔离。rank_cap(L301)同样只认棋名。
  - **实锤问题(2026-08-06 检查):** 磁盘 `/root/.nanobot/agents/*/config.json` 里 rank 全是现代四级名:
    - beholder=basic, benjamin=basic, lucas=basic, retriever=basic
    - harper=standard, scholar=standard, verifier=standard
    - kirk=advanced
  - 当前代码 `rank_cap.get("basic",3)` 和 `_RANK_ORDER.get("basic",0)` 都取**默认值** → rank 隔离实际全部回落到最低档(播送容量默认 3、rank=pawn 同等)。**磁盘配置与代码命名不兼容,当前版隔离未起作用。**

- **memory_palace**
  - 5/27:仅在 agent 配置/session 覆盖显式启用才注册,且有 `ForgetTool`。
  - 当前版:无条件注册,删掉 ForgetTool 和 enable-disable 逻辑。

- **agent_ranks 计算**
  - 5/27:用 `compute_agent_ranks`(visibility 模块)提前算好传进 BroadcastView。
  - 当前版:内联计算,只用于工具隔离;leader 强制 max+1。

---

## 三、其他逻辑改动

- **历史剪枝**:5/27 有 tool_loop 前"预剪 + summarize"覆盖所有路径;当前版移除预剪,改剪枝后**刷新 `_sys_msg_count`**,避免误删系统提示前缀。
- **清理收尾**:5/27 用 `asyncio.wait(timeout=3)`;当前版 `gather(return_exceptions=True)` + 二次 cancel,保证无孤儿任务漏到下一轮(对应 171b4345d "8 logic bugs")。
- **中断计数**:5/27 打断后 reset to 0;当前版 `max(0, count-1)` 更保守。
- **摘要请求**:5/27 静默丢弃 `__SUMMARY__`;当前版转发到 `engine._summary_requested` 由 run_loop 处理。
- **显示标签**:`Output [cycle]` → 当前 `进展 [cycle]`(leader)/ `Self/Final`(其他);`🧵 对话池` → `threads`。
- **锁**:`search_pool._lock` 改为 `async with`(lock async-with 修复)。

---

## 四、待办建议

1. **rank 命名不兼容(`basic`↔`pawn`)是当前最实际的坑。** 两条路:
   - 把磁盘 config 里的 `basic/standard/advanced` 批量迁移成 `pawn/knight/bishop`(或反过来,代码改用现代名)。
   - 当前版代码里 rank_cap / _RANK_ORDER 补上现代名映射,兼容旧 config,避免动磁盘。
2. 当前版已不兼容 5/27 的 `is_discussion_ended()` 概念——若上线要确保没有旧代码/旧插件仍引用它。
3. Harper `list index out of range`(旧 broadcast L1424)已改为 `logger.exception` 打完整堆栈,下次再现可精确定位。