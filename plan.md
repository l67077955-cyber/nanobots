# nanobot 上下文管理与压缩优化计划

> 创建日期: 2026-09-08
> 类型: 子系统优化路线图（上下文管理 / 历史压缩）
> 状态: **活跃主计划**——2026-09-08 起接任根 `plan.md`；**批次 C0、C1、C2 全部完成**（C0：`83b4b555a` 等 4 commit；C1：`5881a8a37` 等 5 commit；C2：`eb4c00d4d`／`375ca771b`／`adce2566a`+`00a48a96e`／`69a60ee84`，终态 836 passed）；**C3 待真实数据积累后重评**（结论与触发条件见 `docs/compression-vs-cache-2026-09.md` §6）；新行为已随 2026-09-08 17:36 网关重启上线
> 与其他计划的关系:
>   - `docs/plan-2026-09-07-arch-refactor.md`（状态所有权 / broadcast 拆分 / channels
>     收敛）——**架构线，并行推进，本计划不碰**。其 Phase 1 step 3（`flip_running` 退役 +
>     `broadcast_round` 返回 `session_should_stop`）仍是那条线的下一个 checkpoint，
>     2026-09-08 起已派子 agent 开工（本计划仍不碰）。
>   - `docs/plan-2026-09-08-industry-followup.md`——**批次 D（压缩 vs 缓存实证）被本计划
>     吸收并扩充**；批次 A 剩 A2（SDK 2.x）、批次 B/C 不受影响，仍按原文执行。
> 红线遵循: AGENTS.md #1 先写测试再改实现、#2 修根源不堆护栏、#3 加行为写 mod
>   不改核心（事件注册属"加事件"，允许）、#4 删死代码、#6 每逻辑单元 checkpoint

---

## Context — 为什么做这件事

历史模型重构（归档于 `docs/archive/plan-2026-09-07-history-refactor.md`，Phase A-E
**经代码核实全部完成**）落地了"单持久日志 + per-agent 视图 + 独立压缩"。但重构解决的是
**数据模型契约**（可见性隔离、跨轮留存、不丢中段），没有回答两个后续问题：

1. **压缩的经济性从未量化**。`litellm_provider.py` 花力气注入 cache_control 断点 +
   `prompt_cache_key` 追求缓存命中，`_compress_view` 又逐 agent 重写视图中部
   （`context.py:400-408`）——每次压缩后下一轮历史前缀全冷。业界数据称这可能是
   负收益（缓存 token 便宜 ~50x；有生产系统实测记忆召回 92%→58% 而盲评分不变——
   静默失效）。nanobot 没有自己的数字。
2. **压缩子系统自身有几处无争议的缺陷**（见下"弱点清单"）：同轮 N 次近重复摘要调用、
   视图无界增长、摘要配置耦合、零可观测性。这些不依赖实证结论就可以修。

本计划分四批：**C0 先让数据存在（纯增量）→ C1 修无争议缺陷（先测试）→ C2 实证评估
（只测不改）→ C3 拿数据做决策**。C2 吸收原 industry-followup 批次 D 全部内容。

---

## 现状（2026-09-08 逐行核实）

### 链路

```
写入: add_message(sender, content, targets)         context.py:165-255
      ├─ 追加持久日志 self.messages（两步裁剪只作用于日志 :222-253）
      ├─ 追加到 targets 命中的 active agent 的 _views   :194-201
      └─ save_message 落盘（逐条，视图/压缩态不持久化）  persistence.py:176-196

读取: view_for(name) → 持久视图副本，无则按 targets 投影   context.py:257-294
建 prompt: 静态组件（persona/rules 每轮从 manifest 重建，不在历史、结构性免压缩）
      → history_to_messages(view, 100k char 预算，尾部填充，
        全部 user+system 消息+首条视为 critical)      message_converter.py:93-119
      → 易变变量（datetime/round）放末尾（缓存友好布局）  prompt_builder.py:697-775

压缩: compress_all → 逐 active agent compress_for        context.py:296-319
      阈值 len(view) ≥ max_messages × compress_ratio     :341-344
      保护: 首条 + 全部用户消息（head）+ 尾部 keep_recent  :348-355
      中段: age_tool_log 老化 → LLM 摘要（≤600 token）     :359-390
      重写: view[:] = head + 摘要 + tail                   :395-408
      摘要不可用 → 保留中段（不丢）                         :412-414
触发: 群聊每轮末 run_loop.py:167；direct-chat 每次回复后 engine.py:1171-1172
```

轮内另有两条独立减载路线（与上述互不干扰）：tool_loop 确定性剪枝
（`tool_pruning.py:135-205`，软阈值 0.3）与 broadcast 尾部摘要
（`tool_pruning.py:208-320`，**刻意把摘要追加进最后一条 system 消息以保持条数
稳定、护住 DeepSeek 自动前缀缓存** :298-316——这是仓库内已有的"缓存友好压缩"先例）。

### 默认值 vs 本机实际

| 参数 | 仓库默认（history_settings.py:57-81） | 本机 `~/.nanobot/history_settings.json` |
|---|---|---|
| max_messages | 200 | **30** |
| compress_ratio | 0.8（160 条触发） | **0.7（21 条触发）** |
| compression_keep_recent | 20 | 20（默认） |
| compress_max_summary_tokens | 600 | **2000** |
| history_summarize_enabled | true | true（缺省继承） |
| context_window_tokens | 200_000 | **50_000** |
| tool_results.summarize_enabled | true | **false** |

⚠️ 本机 regime 下触发阈值 21 条、尾部保护 20 条 → 可压中段 ≈ 0-1 条，**当前生产
网关的 HistoryContext 压缩近乎 no-op**。gateway.log 里的 726 条"compressed"行
（192+534）来自历史时期不同配置。实证分析必须按时期的 settings regime 分段，不能混算。

### 弱点清单（全部有代码证据）

| # | 弱点 | 证据 | 性质 |
|---|---|---|---|
| W1 | 压缩重写视图中部 → 作废缓存前缀，从未量化 | context.py:400-408 vs litellm_provider.py:218-255, 707-739 | 未知风险 |
| W2 | 同轮 N 个 agent 视图近乎同涨同触发 → N 次内容近同的摘要调用，无去重 | compress_all :311-319；多数消息 targets=["All"] 同步进入所有视图 | 纯浪费 |
| W3 | `add_message` 两步裁剪只作用于日志，**视图无界**；摘要禁用时视图只增不减 | context.py:222-253 vs :412-414 | 缺陷 |
| W4 | 视图与压缩态不持久化；且 `_active_agents` 只在首次压缩触发时才设置，首轮读全走临时投影 | persistence.py:176-196；engine.py:973-981 | 已知取舍，可重评 |
| W5 | governance decay：历史中段的"系统"消息（话题公告/注入警告）可被压掉；摘要提示词不要求保留约束；零测试 | context.py:348（head 只保首条+用户消息）, :371-376（提示词）| 安全面 |
| W6 | 压缩调用不带 metadata → request_logs 里 `agent=null, mode=null`，归因靠启发式猜 | context.py:380-384 | 可观测性 |
| W7 | request_logs **不落 cost / cache_tokens**（只有 prompt/completion/total） | litellm_provider.py:290-393 写入侧 vs :1100-1156 解析侧（字段已解析、仅未持久化） | 可观测性 |
| W8 | 事件总线无 `history:compressed` 事件，mod/telemetry 看不见压缩 | events.py:31-52 | 可观测性 |
| W9 | history 压缩复用 `tool_results.summarize_model`，无独立配置 | context.py:338；history_settings.py:162-163 | 配置耦合 |
| W10 | 零召回率测量；`test_compression_pipeline_snapshot.py` 是单行 `str` stub；`test_hard_cap_breaks_keep_recent` 传已删除的 `hard_max_total_chars` 参数（被 pyproject.toml:122-127 有意 deselect 掩盖） | tests/；tool_pruning.py:135-142 | 死代码+盲区 |
| W11 | `context_validator.py` / `cache_probe.py` 零测试 | — | 盲区（低优） |

---

## 批次计划

排序原则：**纯增量的可观测性先做（否则 C2 无数据）→ 无争议缺陷修复（不依赖实证结论）
→ 实证（只测不改）→ 数据驱动决策（可能不动）**。

### 批次 C0：可观测性基建 ✅ 已完成（2026-09-08；原 🔴 前置——纯增量，不改任何现有行为）

> **完成记录**：`83b4b555a`（C0.1）／`4227673f7`（C0.2）／`0a5057920` + `3ec6d422f`（C0.3 含
> round_telemetry 订阅）。新增 26 个测试（`test_request_log_schema` 10、
> `test_history_compress_metadata` 4、`test_history_compressed_event` 12），全量
> `751 passed, 32 deselected`。实现要点：C0.2 沿用仓库既有 `log_agent`／`log_mode`
> metadata 惯例（测试钉真实 `_log_request` 映射；压缩=`history_compress`、尾部摘要=
> `tail_summarize`）；C0.3 经 `get_bus()` 单例 lazy import 发事件（避免 runtime↔history
> 循环导入），`triggered_by` 走关键字参数默认值，run_loop 调用点零改动（显式传参会
> 破坏 `test_run_loop_session_state.py` 钉住的无 kwargs 签名）。遗留一行清理：
> `channels/telegram/commands/log.py:151` 读顶层 `cache_tokens`（历来无写入方的死读取），
> 应改读 `usage.cache_tokens`——记入 C1.4。

1. **request_logs 补 `cost` + `cache_tokens` 字段**（W7）
   - `litellm_provider.py` `_log_request`/`_log_stream_request`（:290-393, :419+）在
     success 条目追加 `cost` 与 `usage.cache_tokens`——两个字段在 `_parse_response`
     （:1100-1156）已解析进 `LLMResponse`/`provider_meta`，只差持久化。`httpx_provider.py:465+`
     同步。旧条目读取按缺省 None 兼容。
   - **先写测试**（新建 `tests/test_request_log_schema.py`，真对象+微小假件风格，
     参考 `tests/test_user_ingress.py`）：钉住 success 条目含 `usage.prompt/completion/total`
     + `cache_tokens` + `cost`，error 条目不含。
2. **压缩调用携带 metadata**（W6）
   - `context.py:380-384` 与 `tool_pruning.py:281-290` 的摘要调用加
     `metadata={"log_mode": "history_compress", ...}`，让 request_logs 条目可归因
     （顺带给压缩调用带来 `prompt_cache_key`——同一会话连续压缩可命中缓存）。
   - 测试钉住：摘要请求的日志条目 `mode == "history_compress"`。
3. **事件总线加 `history:compressed`**（W8，AGENTS #3 允许的"加事件"）
   - `events.py` EVENTS 注册 `history:compressed`，payload:
     `{agent, dropped, view_before, view_after, model, prompt_tokens, completion_tokens,
     cost, triggered_by: round_end|direct_reply}`；`context.py:409` 处 emit。
   - `round_telemetry` mod 顺带加一个订阅 handler（可选，一个逻辑单元一个 commit）。

### 批次 C1：无争议缺陷修复 ✅ 已完成（2026-09-08；原 🟡 每项：先测试 → 改实现 → checkpoint）

> **完成记录**：5 commit——C1.1 视图上限 `5881a8a37`（`_trim_to_limits` 抽取，日志与
> 每个已物化视图按**各自的** head 保护裁剪，未物化视图走日志投影天然有界；6 测试）；
> C1.2 同轮去重 `4d0d739d9`（单次 `compress_all` 批内按 `(prompt, model, max_tokens)`
> 分组——prompt 逐条渲染中段、是内容全等的代理；1 次调用/N 个事件；失败不缓存、
> standalone `compress_for` 永不共享；隐私论证：键覆盖中段全内容，同组视图所见完全
> 相同，共享摘要不可能跨进缺少源材料的视图；6 测试）；C1.3 `74224693d`
> （`history.summarize_model` 缺省回退 `tool_results.summarize_model`，空串视同未设；
> 6 测试）；C1.4 `63c837398`＋`8f6835323`（str-stub 删除、死参数测试改写为现行软剪枝
> 语义、deselect 32→31、`/log` 读 `usage.cache_tokens`；4 测试）。全量终态
> `774 passed, 31 deselected`（我独立复跑确认）。已知取舍：视图上限 = `limit + 窗口外
> 保护 head 条数`（与日志裁剪现状一致，非绝对 `≤ max_messages`——首条恒保护下后者
> 数学上不可达）；C1.2 有意改变了"每视图一次调用"的旧语义，`test_history_compress_
> metadata.py` 随之适配（同组共享调用归因组首成员）。

1. **视图上限**（W3）：`add_message` 的消息数/char 两步裁剪同样作用于每个
   `_views[name]`（裁剪语义：优先丢最旧的非保护消息，与 `_compress_view` 的
   head/tail 保护一致）。测试：摘要禁用 + 持续写入 → `len(view) ≤ max_messages`，
   且用户消息与首条仍保留。
2. **同轮重复摘要去重**（W2）：`compress_all` 内对 (中段内容 hash + 摘要参数)
   分组，相同组只发一次 LLM 调用、共享 `summary_msg`；含私有段（targets 命中
   不同）的视图天然 hash 不同、各自压——**隐私不变量不受影响**（补测试钉住：
   两 agent 视图相同时 provider 只被调一次；含 A→B 私有段时仍两次、C 的视图
   不含该摘要）。
3. **history 独立摘要模型**（W9）：`history_settings` 新增 `history.summarize_model`，
   缺省 `None` → 回退 `tool_results.summarize_model`（向后兼容，测试钉回退）。
4. **死代码清理**（W10，AGENTS #4）：删除 `tests/test_compression_pipeline_snapshot.py`
   （内容是字面量 `str` 的 stub，所防护的 pipeline/stages 重构从未发生）；审查
   `pyproject.toml:122-127` deselect 列表中锁着已删 API 的测试
   （`test_hard_cap_breaks_keep_recent` 传不存在的 `hard_max_total_chars`）——删除或
   改写为现有 `prune_messages` 签名，同步收缩 addopts。commit message 写明证据。
   顺带修 `channels/telegram/commands/log.py:151`：读顶层 `cache_tokens`（零写入方的
   死读取，C0.1 起真实数据在 `usage.cache_tokens`）。

### 批次 C2：实证评估 ✅ 已完成（2026-09-08；原 🔴 只测不改——吸收 industry-followup 批次 D）

> **完成记录（C2.1-C2.5 全部经主会话独立复核；结论以 `docs/compression-vs-cache-2026-09.md` 为准）**：
> - **C2.1** `eb4c00d4d`：`scripts/analyze_compression_cache.py`（977 行）+ 29 测试（合成
>   fixture）。本机 122 天/64,090 条目实跑：归因 431 次压缩摘要调用（全部 marker 启发式；
>   C0 真实字段 0 条 cost/0 条命中/0 条 mode 标记）；12 个 settings regime 分段；净收益
>   Σ(C×(M−S))=+36.1M token，但按"上下界同号才稳健"仅 4 小段稳健为正、S8 稳健为负、
>   最大段 S2 与当前段 S12 方向不定——**本机数据尚不能判定压缩是否值得**；5 次 S>M
>   膨胀事件中 4 次在当前 regime 段（30/0.7/20 阈值却配 2000 token 摘要上限的错配画像）。
>   重启网关后重跑本脚本即自校准。
> - **C2.2** `375ca771b`：`tests/compression_recall_metric.py`（可复用召回 kit，stdlib
>   零依赖）+ 20 测试。确定性数字：60 条视图/10 事实（8 中段+2 尾部），摘要 keep=3→
>   召回 0.5、keep=0→0.2、keep≥8→1.0；同区域确定性截断恒 0.2。即**摘要压缩质量
>   完全取决于摘要保留了多少事实，最差时精确等于盲截断**。
> - **C2.3** `adce2566a`+`00a48a96e`：先钉暴露面（中段系统约束压缩后不可原文检索；
>   `view_for_raw` 仍持原文）+ persona/hard_rules 结构性安全（正控制+防回归）；缓解选
>   **选项 A**（摘要提示词加"约束/规则/禁令逐字保留"指令，context.py +4 行）。选项 B
>   （系统消息入 head 保护）被否的决定性证据：压缩摘要自身 sender=系统，入保护则摘要
>   永久堆积、废掉压缩；且现有中段系统消息只有一个生产者（run_loop.py:108 话题公告），
>   标记护不住未来措辞。诚实局限：防护强度依赖模型服从性，已钉进测试。
> - **C2.4+C2.5** `69a60ee84`：对照实验（8 测试，与 C2.2 同布局互钉锚点，防两文件漂移）
>   + 结论文档。关键澄清：**缓存前缀发散点两路线相同**（都在第一个非保护位）——
>   "换截断救缓存"不成立，缓存友好的唯一杠杆是重写点位置；截断召回恒为保护下限，
>   摘要 k≥1 即严格占优（k=0 时两者等同）；对 C3 四选项的预判：换路线——数据不支持、
>   仓库默认值——不动、缓存友好改造与持久化——暂缓等 C0、本机 regime 近 no-op——
>   是数据事实但修正方向留用户。
> - 终态全量 `836 passed, 31 deselected`（主会话独立复跑）。网关 2026-09-08 17:36 idle
>   重启（用户经 AskUserQuestion 显式授权；启动序列干净，log tail 中的 ERROR/重复警告
>   为并行测试进程经 loguru sink 的已知污染），C0+C1+C2.3 行为上线，request_logs 自此
>   积累真实 cost/cache_tokens/mode；gateway.log 的 `compressed` 行不再作生产计数
>   （结论文档附录 A-5）。

1. **离线成本分析**：新建 `scripts/analyze_compression_cache.py`，输入
   `~/.nanobot/request_logs/*.jsonl`（122 天全量）+ `gateway.log` 压缩时间戳
   （726 条），按 settings regime 分段（见上表警告），输出每次压缩事件前后的：
   - cache 侧：`cache_probe` 估计命中率（历史条目）与 C0 后的真实
     `cache_tokens` 占比变化；
   - 成本侧：摘要调用自身的 token/cost（C0 后直接归因；历史条目按
     `model+msg_count=1+"中期历史记录"` 启发式重建，并标注置信度）；
   - 净收益判定：压缩节省的输入 token 费用 vs (摘要调用费用 + 缓存失效损失)。
2. **召回率测量**：新建 `tests/test_compression_recall.py`——植入 N 个事实
   （数字/路径/决策），跑压缩（假 provider 返回确定性摘要，可控地丢弃部分事实），
   度量压缩前后 `view_for` 对事实的召回率。产出可复用的召回度量函数——
   做 nanobot 自己的"92% vs 58%"。
3. **governance decay 防护**（W5）：
   - 测试先行：钉住现状——中段"系统"消息（话题公告/约束）压缩后在视图中不可
     原文检索（暴露面文档化）；
   - 然后二选一（按测试结果定）：摘要提示词（context.py:371-376）加"逐字保留
     约束/规则类语句"指令 + 钉住摘要保留约束的测试；或将含约束标记的"系统"
     消息纳入 head 保护（行为改动，需单独 checkpoint）。
   - 注：persona/hard_rules 每轮从 manifest 重建、不在历史里，**结构性安全**——
     测试同时钉住这一点（防回归）。
4. **对照替代路线**：用 (2) 的召回度量 + (1) 的成本模型，并排比较
   "摘要压缩（HistoryContext 路线）vs 确定性截断（tool_pruning 路线）"的
   成本/召回曲线。
5. **产出**：`docs/compression-vs-cache-2026-09.md`，给出结论与建议
   （动不动默认值、要不要换路线）。**代码零改动**（除测试与脚本）。

### 批次 C3：数据驱动的决策 ⚪（C2 结论出来才排期，可能全部不做）

按 C2 数据从下列选项中挑（或全不挑）：

- **缓存友好压缩改造**：把 `view[:] = rebuilt` 的中段重写改为"摘要追加进最后一条
  受保护消息"（`tool_pruning.py:298-316` 已有先例），把缓存失效点后移/收窄。
  仅当 C2 证明缓存损失 >> 压缩收益时做。
- **阈值/路线调整**：上调触发阈值，或对自动前缀缓存 provider（DeepSeek 类）走
  确定性截断而非 LLM 摘要。改 `history_settings.py` 默认值必须引用 C2 数据。
- **视图/压缩态持久化**（W4）：重启不丢压缩态。上轮计划标"不做"，C2 若显示
  重启后重压缩造成显著重复成本则重评。
- **本机 regime 修正**：若 C2 确认"30/0.7/20 近乎 no-op"不是有意为之，
  修 `~/.nanobot/history_settings.json`（运维动作，不是代码改动）。

---

## 验证（DoD）

| 批次 | 验收 |
|---|---|
| C0 | 新 schema/事件测试绿；关掉新字段写入时旧行为不变；`pytest tests/ -q` 全绿 |
| C1 | 每项先有失败测试再有实现；W2 去重后同轮摘要调用数从 N → 内容分组数；W3 视图有界测试绿；stub 与陈旧测试删除、addopts 收缩后全绿 |
| C2 | `scripts/analyze_compression_cache.py` 在本机 122 天数据上跑通并输出分段报告；`tests/test_compression_recall.py` 绿；`docs/compression-vs-cache-2026-09.md` 存在且结论有数据引用 |
| C3 | 每个被选中的选项单独 checkpoint，引用 C2 数据为 why |
| 全部 | 每逻辑单元一个 commit（what+why+证据）；动到线上行为时：agent idle → 重启网关 → 观察 gateway.log 首轮 |

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| C0 改 `_log_request` 影响生产日志体积 | 低 | 低 | 字段仅在 success 且有值时写入；旧读取方按缺省兼容 |
| C1.2 去重误判"相同"导致跨视图泄露 | 低 | 高 | 去重键=中段内容逐条 hash 全等；差一条即各自压；测试钉隐私不变量 |
| C1.1 视图裁剪丢掉仍需要的消息 | 中 | 中 | 裁剪语义与压缩保护一致（首条+用户消息+尾部），测试钉保护集 |
| C2 历史数据 regime 混杂导致结论失真 | 高 | 中 | 按 settings 变更时点分段；启发式归因的条目标注置信度 |
| 与架构线（`docs/plan-2026-09-07-arch-refactor.md` Phase 1 step 3）并行冲突 | 中 | 中 | 两条线改动面不重叠（本计划不碰 runtime 状态机）；均需全量测试绿才提交 |

## 不做（本轮范围外）

- 架构线 `docs/plan-2026-09-07-arch-refactor.md` Phase 1/2/3 的任何条目（那条线自己推进）
- industry-followup 批次 A2（mcp SDK 2.x）、B（OTel mod）、C（cost guard mod）
  ——其中 B/C 与 C0 的 `llm:*` 事件/成本归因有协同，但各自按原文排期
- mailbox/引擎状态机（上轮已收敛）
- 跨组织协议（A2A/ACP）——industry 计划已判"观望"

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-09-08 | 创建本计划。基于三路并行审计：①压缩链路逐行核实（W1-W11 弱点清单）；②三份既有计划完成度核实（history-refactor A-E 真完成；根 plan.md Phase 1 完成 2/4 步、step 3 未动；industry 批次 A 完成 1/3、A4 改道、B/C/D 零产出）；③本机数据可用性核实（request_logs 122 天但缺 cost/cache_tokens → 批次 D 原设想的离线分析不可直接跑，故新增 C0 前置）。吸收 industry-followup 批次 D 为本计划 C2。 |
| 2026-09-08 | 接任根 `plan.md` 成为活跃主计划；原架构线计划（状态所有权 / broadcast / channels，Phase 1 进行中）移至 `docs/plan-2026-09-07-arch-refactor.md` 继续推进，未归档。 |
| 2026-09-08 | 批次 C0 完成（两个子 agent 并行，4 commit，26 新测试，全量 751 passed / 32 deselected）：request_logs 落 `cost` + `usage.cache_tokens`；两条压缩路线摘要调用分别带 `history_compress`／`tail_summarize` 归因；EVENTS 注册并 emit `history:compressed`，round_telemetry 订阅。网关重启因 agent 活动暂缓，待 idle 执行。 |
| 2026-09-08 | 批次 C1 完成（两个子 agent 并行，5 commit，23 个新/复活测试，全量 774 passed / 31 deselected，主会话独立复跑确认）：W3 视图随 `add_message` 有界；W2 同轮相同中段共享一次摘要调用（隐私不变量有测试钉住）；W9 `history.summarize_model` 独立旋钮带回退；W10 str-stub 删除 + deselect 收缩 + `/log` 死读键修正。执行注记：并行 agent 共享 git index 出现一次良性竞态（一方 staged 删除被另一方 pathspec commit 吸收，终态正确、证据在删除方 commit message）；后续并行提交一律 `git commit -- <paths>`。网关重启仍待 idle 与用户显式指令。 |
| 2026-09-08 | 批次 C2 中间结果落盘（三个子 agent 并行，C2.1-C2.3 完成，54 新测试，全量 828 passed / 31 deselected）：成本分析器本机实跑（431 次归因调用、12 分段、净收益方向多数不定）；确定性召回度量（keep→召回纯函数，截断恒 0.2）；governance decay 选项 A 落地。C2.4+C2.5（对照+结论文档）在途。 |
| 2026-09-08 | 批次 C2 完成（C2.4+C2.5 `69a60ee84`，8 新测试，终态 836 passed / 31 deselected，主会话独立复跑确认）；网关 17:36 idle 重启上线 C0+C1+C2.3 行为——重启前验证：最后活动 17:28（>5 min idle）、tracked 运行代码零改动恰为 828 全绿 HEAD（仅两个未跟踪 docs/tests 文件，网关不 import，不构成部署风险）。C3 进入等数据状态，重评触发条件见结论文档 §6.5。 |
| 2026-09-08 | 并行两线收尾并经主会话独立复核（diff 逐块 + 全量独立复跑）：industry 批次 B+C（`e7752e89b` llm:request/response 事件、`316611791` otel_export、`33f072c21` cost_guard，两 mod 均默认关、gen_ai.* 收敛单点）与架构线 Phase 1 step 3（`abf36831b`+`67eb54d67`+`5f0e51137`，flip_running 退役、轮次判定经 RoundResult 数据化）落地；全量 **874 passed / 31 deselected**。注意：17:36 网关重启早于这批 commit——生产仍在跑 C0+C1+C2.3 代码，B1 事件发射与架构线重构待下一次 idle 重启部署（按既定纪律需用户显式点名）。 |
