# 代码优化计划 (TODO)

> 基于对 `nanobot/` 核心代码的抽样审查（session / tools / bus / channels / telegram），共约 34k 行 Python。
> 每项包含：缺陷描述、位置、修改方法、难度、优先级。
>
> **状态标记**: ✅ 已完成 | ⚠️ 部分完成 | ❌ 未开始

---

## 1. 会话保存是全量重写，追加消息代价 O(n)  ✅ 已完成

- **位置**: `nanobot/session/manager.py` → `SessionManager.save()`
- **缺陷**: 号称 "JSONL append-only for cache efficiency"，但 `save()` 每次都用 `open(path, "w")` 整文件重写（metadata 行 + 全部消息）。长会话（数千条消息）时每轮对话都全量写盘，I/O 放大且崩溃时可能截断丢失整个会话。
- **修改方法**:
  1. 拆分为 `append_message()`（`open(path, "a")` 追加单行）+ 低频的 `rewrite()`（consolidation 时才全量重写）。
  2. metadata 移到独立 sidecar 文件（如 `xxx.meta.json`）或写在文件尾部由加载时归并，避免 metadata 在首行导致必须重写。
  3. 全量重写时先写 `xxx.jsonl.tmp` 再 `os.replace()` 原子替换，防止半写状态。
- **难度**: ★★☆（中，需要同步改 `_load` 与所有调用点，注意与 consolidation 逻辑兼容）
- **优先级**: 高
- **完成情况**: 已实现 `append_message()` 增量追加（O(1)）+ `save()` 原子写入（tmp + os.replace）+ `_rewrite_metadata()` 单独更新首行

## 2. 会话文件同步 I/O 阻塞事件循环  ✅ 已完成

- **位置**: `nanobot/session/manager.py` → `_load()` / `save()` / `list_sessions()`
- **缺陷**: 全部是同步 `open()` 读写，而项目整体是 asyncio 架构（`MessageBus`、channels 均为 async）。大会话文件加载/保存会卡住事件循环，影响所有 channel 的消息处理。
- **修改方法**: 在调用侧用 `asyncio.to_thread(manager.save, session)` 包装；或将 `save/_load` 改为 async 并内部 `to_thread`。配合第 1 条的增量追加后阻塞时间会大幅缩短。
- **难度**: ★☆☆（低）
- **优先级**: 高
- **完成情况**: 已添加 `save_async()` / `append_message_async()` 异步包装器，内部用 `asyncio.to_thread` 执行

## 3. shell 工具的 deny_patterns 黑名单容易绕过  ⚠️ 部分完成

- **位置**: `nanobot/tools/shell.py` → `ExecTool.__init__` / `_guard_command`
- **缺陷**: 用正则黑名单拦截 `rm -rf`、`dd` 等，但黑名单机制天然可绕过：`command="r""m -rf /"`、`bash -c "$(echo cm0...|base64 -d)"`、变量拼接、`xargs rm` 等均不命中。给用户安全错觉。
- **修改方法**:
  1. 文档中明确这只是"提示性护栏"而非安全边界。
  2. 提供可选的强隔离模式：`restrict_to_workspace=True` 时用 `bwrap`/`docker`/受限用户执行；或至少默认启用 `restrict_to_workspace`。
  3. 黑名单补充覆盖 `xargs`、`find -delete`、`chmod -R 000` 等常见变体（治标）。
- **难度**: ★★★（高，涉及安全模型设计；仅补正则则 ★☆☆）
- **优先级**: 高
- **完成情况**: ✅ 文档已加安全声明（非安全边界）✅ 默认 `restrict_to_workspace=True` ✅ 黑名单已补充 xargs/find -delete/chmod 变体 ❌ 强隔离模式（bwrap/docker）未实现

## 4. `except Exception` 泛捕获过多  ⚠️ 部分完成

- **位置**: 全仓库，`grep -rn "except Exception" nanobot` 基线（merge-base 9c7e253）共 294 处；典型如 `session/manager.py:list_sessions()` 中 `except Exception: continue`（静默吞错）。
- **缺陷**: 大量宽泛捕获且部分不记日志，掩盖真实 bug（如 JSON 损坏、权限问题），排查困难。
- **修改方法**: 分批治理——
  1. 先加 lint 规则（ruff `BLE001`/`S110 try-except-pass`）盘点。
  2. 静默吞错处至少补 `logger.warning/exception`。
  3. 能明确的改为具体异常类型（`json.JSONDecodeError`、`OSError`）。
- **难度**: ★★☆（中，量大但机械，可分模块分批做）
- **优先级**: 中
- **完成情况**: 
  - ⚠️ 总数从 294 减少到 287（净减 7，约 2.4%；主要工作是**将静默吞错改为记日志**，捕获总数基本持平，未大量收窄为具体异常类型）
  - ✅ 静默吞错（`except Exception` 后紧跟 pass/continue 且无日志）从 71 减少到 28（减少 43，约 61%）
  - ✅ 已修复模块：providers/、groupchat/orchestra/、groupchat/history/、groupchat/display/、utils/、tools/
  - ⚠️ 剩余主要在 channels/telegram/callbacks.py（UI toast 失败静默处理）
  - ❌ 未启用 ruff BLE001 规则（可选后续）

## 5. telegram/callbacks.py 巨型文件 + 巨型 if/elif 前缀路由  ⚠️ 部分完成

- **位置**: `nanobot/channels/telegram/callbacks.py`（2910 行）→ `_on_callback()`（91–140 行的多行超长 `data.startswith(...) or ...` 链）
- **缺陷**: 单文件近 3000 行、单个 elif 条件含 20+ 个 `startswith`，新增回调极易漏注册/前缀冲突（如 `rlog:` vs `rlogctx:` 依赖排列顺序），可读性和可测试性差。
- **修改方法**:
  1. 建 `CALLBACK_ROUTES: dict[str, Handler]` 前缀路由表，`_on_callback` 按最长前缀匹配分发。
  2. 按功能拆文件：`callbacks/agent_ops.py`、`callbacks/hyperparams.py`、`callbacks/provider_models.py` 等，保持 Mixin 组合或改组合模式。
  3. 为路由表加单测：断言所有前缀无歧义覆盖。
- **难度**: ★★☆（中，纯重构无行为变更，但改动面大需回归测试）
- **优先级**: 中
- **完成情况**: ✅ 已拆分出 `_handle_agent_ops` / `_handle_hyperparams` / `_handle_prompt_edit` / `_handle_provider_models` / `_handle_edit_and_logs` 等方法 ❌ 文件仍 2910 行，未拆分到多文件 ❌ 未建路由表

## 6. MessageBus 队列无上限，无背压  ✅ 已完成

- **位置**: `nanobot/bus/queue.py` → `MessageBus.__init__`
- **缺陷**: `asyncio.Queue()` 未设 `maxsize`。若 agent 处理慢或卡死，inbound 无限堆积（内存膨胀），且发送方无任何反馈。
- **修改方法**: `asyncio.Queue(maxsize=N)`（如 1000），`publish_inbound` 提供 `put_nowait` + 满时丢弃/告警的策略，或保留 await 阻塞形成天然背压；补充队列水位日志。
- **难度**: ★☆☆（低）
- **优先级**: 中
- **完成情况**: ✅ `asyncio.Queue(maxsize=1000)` 有上限 ✅ 水位日志（80%/95%告警）✅ `publish_inbound` 满时阻塞形成背压

## 7. Feishu WebSocket 用独立线程 + `time.sleep(5)` 重连  ✅ 已完成

- **位置**: `nanobot/channels/feishu.py` 约 340–360 行 `run_ws()`
- **缺陷**: 在线程里 new event loop 并 monkey-patch `lark_oapi.ws.client.loop`（依赖第三方库内部实现，升级即碎）；固定 5s 重连无退避；`self._running` 跨线程读写无同步。
- **修改方法**:
  1. 重连改指数退避 + 抖动（5s → 最大 60s）。
  2. `_running` 改 `threading.Event`。
  3. 给 monkey-patch 加版本守卫注释/try 检测，lark SDK 若支持传入 loop 则改用官方 API。
- **难度**: ★★☆（中，受第三方 SDK 限制）
- **优先级**: 中
- **完成情况**: ✅ 指数退避 5s→10s→20s→40s→max 60s + 10% 抖动 ✅ `threading.Event` 替代 bool `_running` ✅ 连接成功时重置重试计数器

## 8. `Session.get_history` 每次调用重复做全量扫描  ✅ 已完成

- **位置**: `nanobot/session/manager.py` → `get_history()` / `_find_legal_start()`
- **缺陷**: 每轮对话都对切片后消息做两遍线性扫描（找 user 起点 + 找 legal tool-call 边界），`_find_legal_start` 内还有嵌套回扫（最坏 O(n²)）。消息多时白白消耗 CPU。
- **修改方法**: 缓存上次计算的 `(last_consolidated, len(messages)) → history` 结果，消息只追加时增量维护 declared tool_call id 集合；或简化 `_find_legal_start` 为单遍算法（记录每个 orphan tool 结果的位置，取最大值+1）。
- **难度**: ★★☆（中，需保证与现有单测 `test_consolidate_offset.py` 等兼容）
- **优先级**: 低
- **完成情况**: ✅ 添加 `_history_cache` 字段缓存结果 ✅ 基于 `(last_consolidated, msg_count)` 判断缓存有效性 ✅ `clear()` 时清除缓存

## 9. `list_sessions` 的 key 反解析不可靠  ✅ 已完成

- **位置**: `nanobot/session/manager.py` → `list_sessions()` 中 `path.stem.replace("_", ":", 1)`
- **缺陷**: 文件名由 `key.replace(":", "_")` + `safe_filename` 生成，是有损转换；当 chat_id 本身含 `_` 或被 safe_filename 改写时，反解析得到错误 key（仅在 metadata 缺 key 时触发，但属静默数据错误）。
- **修改方法**: metadata 行已存 `key`，将无 metadata 的旧文件视为异常并记 warning；或在文件名中使用可逆编码（如 urlsafe base64）。
- **难度**: ★☆☆（低）
- **优先级**: 低
- **完成情况**: ✅ metadata 已存储 `key` 字段（原有）✅ 缺少 key 时记录 warning（原有）✅ 优化 warning 消息说明潜在问题

## 10. 批量 exec 的 `commands` 并发无上限  ✅ 已完成

- **位置**: `nanobot/tools/shell.py` → `execute()` batch 模式 `asyncio.gather(*tasks)`
- **缺陷**: LLM 可一次传入任意多条命令并发执行，无并发数限制，可能耗尽进程/文件句柄。
- **修改方法**: 用 `asyncio.Semaphore(4~8)` 包装 `_run_one`，并限制 `commands` 数组长度（如 ≤16，schema 加 `maxItems`）。
- **难度**: ★☆☆（低）
- **优先级**: 低
- **完成情况**: ✅ `asyncio.Semaphore(8)` 限制并发数 ✅ `_MAX_BATCH_COMMANDS = 16` 限制批量命令数

---

## 完成情况汇总

| # | 项目 | 状态 | 备注 |
|---|------|------|------|
| 1 | 会话保存全量重写 | ✅ 已完成 | append_message + 原子写入 |
| 2 | 会话同步 I/O 阻塞 | ✅ 已完成 | asyncio.to_thread 包装 |
| 3 | shell deny_patterns | ⚠️ 部分完成 | 文档+默认值+黑名单补充，强隔离未做 |
| 4 | except Exception 泛捕获 | ⚠️ 部分完成 | 294→287 处（净减 7），静默吞错 71→28 |
| 5 | callbacks.py 巨型文件 | ⚠️ 部分完成 | 方法已拆分，文件未拆 |
| 6 | MessageBus 队列无上限 | ✅ 已完成 | maxsize=1000 + 水位日志 |
| 7 | Feishu WebSocket 重连 | ✅ 已完成 | 指数退避 + threading.Event |
| 8 | get_history 重复扫描 | ✅ 已完成 | 添加缓存字段 |
| 9 | list_sessions key 反解析 | ✅ 已完成 | metadata 存 key + warning |
| 10 | 批量 exec 并发无上限 | ✅ 已完成 | Semaphore + 限数 |

---

## 建议执行顺序（更新）

### 已完成（可归档）
- ✅ #1、#2、#6、#7、#8、#9、#10

### 部分完成（需跟进）
- ⚠️ #3：强隔离模式可单独立项
- ⚠️ #4：剩余 30 处静默吞错主要在 channels/telegram/callbacks.py（UI toast 失败）
- ⚠️ #5：可继续拆分到多文件 + 建路由表