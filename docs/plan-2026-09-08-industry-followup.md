# nanobot 业界热点技术跟进计划（2026-07 ~ 2026-09）

> 创建日期: 2026-09-08
> 类型: 技术跟进路线图（不是架构重构）
> 与其他计划的关系: **不挂钩**。架构债（状态所有权 / broadcast 拆分 / channels 收敛）由
>   `docs/plan-2026-09-07-arch-refactor.md` 管；根 `plan.md`（《上下文管理与压缩优化计划》）
>   已吸收本计划批次 D。各线并行，互不阻塞。
> 红线遵循: AGENTS.md #1 先写测试再改实现、#3 加行为写 mod 不改核心、#6 每逻辑单元 checkpoint

---

## Context — 为什么现在做这件事

nanobot 近 4 周 48 个 commit 全部是向内的（fix 11 / feat 11 / refactor 10 / docs 8，
热点集中在 `groupchat` 90 次、`channels` 60 次），没有一次是响应外部生态变化的。
同期业界发生了几件**有外部时钟**的事——MCP 规范在 2026-07-28 换了协议内核并给旧传输
挂上了弃用倒计时，OTel GenAI semconv 成了 coding agent 的事实标准，EU AI Act 的透明度
义务 2026-08-02 生效。这些不跟进不会立刻坏，但会在某个版本悄悄坏掉，或者在需要
「拿出审计记录」时拿不出来。

本计划的目的：把「业界近几周真正变了的东西」筛一遍，只挑对 nanobot 有实际落点的，
排出优先级和批次。**它本身不改架构，绝大部分改动走 mod。**

---

## 一、调研结论：2026-07~09 真正变了的事

按对 nanobot 的相关度排序，标注信源可靠度。

| # | 变化 | 日期 | 信源 |
|---|---|---|---|
| 1 | **MCP 2026-07-28 规范**：协议核心从有状态转为无状态——删除 `initialize`/`initialized` 握手和 `Mcp-Session-Id`，改为每个请求在 `_meta` 里自描述；新增 `Mcp-Method`/`Mcp-Name` 头做网关路由；MRTR 取代服务端主动流；list 响应带 `ttlMs`/`cacheScope` 支持客户端缓存；DCR 被 CIMD 取代；**legacy HTTP+SSE 传输进入弃用（≥12 个月窗口）** | 2026-07-28 | 一手（MCP 官方博客） |
| 2 | **OTel GenAI semconv** 成为 agent 可观测性事实标准，v1.41 定义 agent/workflow/tool/model span + token 用量指标；Claude Code、Copilot、Codex 已直接发 OTLP。**但仍是 pre-stable，无 1.0，属性名还会变** | 2026 上半年成型 | 二手（多来源一致） |
| 3 | **治理/审计成为采购门槛**：EU AI Act 透明度与标注义务 2026-08-02 生效（高风险条款推迟到 2027-12） | 2026-08-02 | 二手 |
| 4 | **模型路由成默认**：最贵/最便宜模型价差被称达 857x，单模型配置经济上不合理 | H1 2026 | 二手（数值存疑，方向可信） |
| 5 | **成本护栏成标配**：agent 原生支付上线，"每个 agent 平台都需要 per-agent 支出上限 + 审计" | 2026 H1 | 二手 |
| 6 | **压缩路线出现反方**：摘要压缩重写缓存前缀、作废 prompt cache（缓存 token 便宜约 50x，压缩要缩 >50x 才回本）；某生产实测**记忆召回 92% → 58%，而盲评质量分仍 97-99%——静默失效**。同期论文讨论 context rot、结构化淘汰、**governance decay（压缩悄悄抹掉安全约束）** | 2026 年中至今 | 二手 + arXiv |
| 7 | **协议分层而非竞争**：MCP 管 agent→工具，A2A 管 agent 间协调，ACP 管复杂协作 | 2026 | 二手 |

只记录、不进计划：开源框架生态碎片化（OpenClaw / Hermes）；Agent Skills 标准已被约 40 个
平台采纳——nanobot 在 2026-09-04 的 `feat(skills): Agent Skills standard frontmatter compat`
已跟上。

### 落地调研补充（2026-09-08 实测）

- **mcp Python SDK 已发布 2.x**：`2.0.0rc1` 卡在 2026-07-27（规范发布前一天），
  `2.0.1`/`2.1.x` 在 8 月下旬，**`2.2.0` 于 2026-09-07 发布**。
- 本机安装的是 **1.26.0**，其 `mcp.types.LATEST_PROTOCOL_VERSION == "2025-11-25"`，
  即仍是 2025 年 11 月的旧规范。`pyproject.toml` 的 pin 是 `mcp>=1.26.0,<2.0.0`，
  **把 2.x 挡在门外**。
- `nanobot/tools/mcp.py` 是唯一的 MCP 客户端实现，被 `runtime/engine.py:224-238`
  调用三次（tools / direct_tools / 每 agent registry），共用一个 `AsyncExitStack`。

---

## 二、对照：nanobot 现状 vs 业界

| 热点 | nanobot 现状（代码证据） | 差距 |
|---|---|---|
| MCP 无状态核心 | `tools/mcp.py:237-238` 走 `ClientSession` + `await session.initialize()`，有状态握手 | 🔴 需跟进 |
| legacy HTTP+SSE 弃用 | `tools/mcp.py:218` 仍支持并自动选择 `sse_client`（URL 以 `/sse` 结尾即启用） | 🟡 弃用窗口内 |
| `tools/list` 缓存 | 无。每次连接全量 `list_tools()`，且被调用 3 次 | 🟡 可优化 |
| OTel GenAI semconv | **零 OTel**。只有 loguru + 两套自定义 JSONL：`mods/builtin/round_telemetry.py`（轮次事件）与 `providers/litellm_provider.py:275`（LLM 请求），schema 各自为政，无 trace 关联 | 🔴 最大空白 |
| 审计追踪 | request_logs 有全量 LLM 请求，但工具调用/轮次在另一文件，拼不出完整因果链 | 🟡 部分 |
| 成本护栏 | `providers/base.py:89` 的 `cost` 只记录；`litellm_provider.py:1130-1155` 带出 cost。**全仓库零预算/零上限强制** | 🔴 无 |
| 模型路由 | 已有 litellm 多 provider + per-agent `model`（`config/schema.py:46`）+ `_match_provider` | 🟢 已具备 |
| Prompt caching | 已有 `_apply_cache_control`（`litellm_provider.py:218-253`）+ `prompt_cache_key`（:738） | 🟢 已具备 |
| 压缩 vs 缓存冲突 | `history/context.py:296-321` 的 `compress_for` 逐 agent 重写历史，**每次都作废上面那套缓存前缀**。二者从未一起量化 | 🔴 未知风险 |
| governance decay | 无任何测试钉住「压缩后系统约束是否还在上下文里」 | 🔴 无 |
| Agent Skills 标准 | `skills/loader.py` 已 frontmatter 兼容 + compact 模式（L1/L2），L3 靠 `read_file` 手动 | 🟢 基本跟上 |
| A2A / ACP | 自研 `mailbox.py`，进程内，零 A2A | ⚪ 观望 |
| 轨迹级评估 | 75 个测试文件，全是确定性单测，无 agent 轨迹/召回评估 | 🟡 空白 |

---

## 三、批次计划

排序原则：**有外部时钟的先做 → 空白最大的次之 → 未知风险做实证 → 无落点不做。**

### 批次 A：MCP 客户端跟上 2026-07-28 规范 🔴 优先

唯一必须动核心的一批（协议兼容属核心 bug 范畴，不是「加行为」，不走 mod）。

1. **先补测试**（AGENTS.md #1）。已有 `tests/test_mcp_tool.py`（10 个用例）覆盖
   `enabledTools` 过滤与 `MCPToolWrapper.execute` 的错误处理（超时、服务端取消、
   外部取消重抛、通用异常），但**transport 选择、握手、注册命名、多 server 失败隔离、
   schema 归一化全部无覆盖**——SDK 升级会是一次盲改。
   新建 `tests/test_mcp_client.py` 补齐这些，不重复已有覆盖。
   风格参考 `tests/test_user_ingress.py`（真对象 + 微小假件，不 mock 内部）。
2. **SDK 升级评估**：`pyproject.toml` 的 `mcp>=1.26.0,<2.0.0` → 2.x。**必须先在隔离
   venv 验证 API 差异**，不能直接升级系统包（生产网关在跑）。向后兼容是硬要求：
   旧 server 仍要能连。
3. **`sse` transport 弃用告警**：`tools/mcp.py` 选中 sse 时 `logger.warning` 一次，
   文档说明迁移到 streamableHttp。**不删代码**（弃用窗口 ≥12 个月）。
4. **`tools/list` 缓存**：按响应的 `ttlMs`/`cacheScope` 落盘 `~/.nanobot/mcp-cache/`。
   SDK 不返回这两个字段时退化为不缓存。注意 `connect_mcp_servers` 被调用 3 次，
   缓存能直接省掉 2 次重复拉取。

**顺带验收目标**：本机两个 MCP server（`/root/my-mcp-server/build/{index,browser}.js`）
当前是 CONNECTION_CLOSED，用它们当端到端验证对象。

### 批次 B：OTel 导出 mod 🔴 空白最大

> **2026-09-08 落地记录**: 已完成。
> ① `events.py` 新增 `llm:request` / `llm:response`（commit e7752e89b），由
> litellm/httpx 两个 provider 在所有 chat / chat_stream 出口路径 emit，
> token/cost 复用 C0 已解析字段（零二次解析）；payload 在计划的 7 个字段外
> 增加了 `session`（per-session 预算键，取自既有 `log_session` metadata）和
> `error`（失败调用）——加字段属兼容变更。
> ② `nanobot/mods/builtin/otel_export.py` 落地，默认关闭，opt-in 走 mods.json
> （`endpoint` / `protocol` grpc|http / `service_name`）；`gen_ai.*` 属性名只存在于
> 该文件的 `_GEN_AI` 映射（docstring 已声明 semconv pre-stable 约束）；
> opentelemetry 全部惰性导入（discovery 无 SDK 也能 import，测试已钉死）。
> ③ `pyproject.toml` 增加 `otel` optional-dependencies 组。
> **验证口径（诚实声明）**：本机无 OTLP collector 且无权安装包，「起真 collector
> 确认端到端投递」**未验证**。已验证的是：SDK InMemorySpanExporter 下的
> workflow→agent→tool→model 完整 span 树与 parenting、属性映射、latency 回填、
> error 状态、mod 关闭时零订阅零导入。真 collector 端到端留作运维侧手工步骤
> （`pip install 'nanobot[otel]'` + mods.json 开启 + 指向 collector 即可）。

走 mod，**零核心行为改动**（AGENTS.md #3）。`mods/builtin/round_telemetry.py` 是现成范式。

1. **前置：给 `events.py` 补 LLM 级事件**。当前 `EVENTS` 最细只到 `tool:result`，
   没有 LLM 请求/响应事件，mod 拿不到 token 用量和 cost。新增 `llm:request` / `llm:response`
   （payload: agent, model, input_tokens, output_tokens, cache_tokens, cost, latency），
   由 `providers/` 侧 emit。这是**加事件，不是加行为**，符合 mod 架构意图。
2. **新 mod `nanobot/mods/builtin/otel_export.py`**，默认关闭，opt-in via mods.json：
   - `round:started`/`round:ended` → workflow span；agent 生命周期 → agent span；
     `tool:result` → tool span；`llm:response` → model span
   - 属性用 `gen_ai.system` / `gen_ai.request.model` / `gen_ai.usage.input_tokens` /
     `gen_ai.usage.output_tokens` / `gen_ai.operation.name`
   - OTLP endpoint 走 mod config
3. **可选依赖**：`[project.optional-dependencies] otel = ["opentelemetry-sdk", "opentelemetry-exporter-otlp"]`。

**关键设计约束**：semconv 仍是 pre-stable（v1.41，无 1.0，属性名会变）。
**只在 mod 导出层做属性映射，绝不把 `gen_ai.*` 命名渗进内部数据结构**——标准改名只改这一个
文件。这条写进 mod docstring。

顺带收益：B 落地后 `round_telemetry.jsonl` 和 `request_logs/*.jsonl` 第一次能靠 trace 拼起来，
「治理/审计」那条也补上大半。

### 批次 C：成本护栏 mod 🔴 当前零防护

依赖批次 B 的 `llm:response` 事件。

- 新 mod `cost_guard`：按 agent / 会话 / 日累计 cost，超阈值告警，再超则让当前轮次收敛。
- 阈值走 mods.json，默认极宽松或关闭（不能让一个 mod 意外掐掉生产网关）。
- **边界先想清楚**：tier-2 规则是「只能往 payload 可变容器 append，不许替换」
  （`mods/base.py` docstring）。「强制结束轮次」超出 tier-2 授权。落地前先定：
  走 `agent:reactivated` 的 `inject` 列表注入软性提示（tier-2 合规但只是建议），
  还是给 mod 系统开明确的 tier-3。**不要偷偷越界戳 engine 内部**——那正是 AGENTS.md #2
  点名的「状态无主」老毛病。

### 批次 D：压缩 vs 缓存的实证评估 🔴 只测不改

> **2026-09-08 更新**: 本批次已吸收进根 `plan.md`《上下文管理与压缩优化计划》
> （成为其批次 C2，并新增了 C0 可观测性前置——审计发现 request_logs 不落
> cost/cache_tokens、压缩调用无标记，原设想的离线成本分析缺数据，直接跑不了）。
> 内容以该计划为准，本节保留作历史索引。

本计划里最可能改变默认配置的一批，但**产出是数据，不是代码改动**。

背景冲突（代码层已确认）：`litellm_provider.py` 花力气注入 cache_control 断点追求缓存命中，
`history/context.py` 的 `compress_for` 又逐 agent 重写历史——每次压缩都作废缓存前缀。
业界数据说这可能是负收益，且召回率下降是**静默的**。nanobot 从未量化过。

1. **成本侧**：拿 `~/.nanobot/request_logs/*.jsonl` 历史数据，算压缩事件前后 `cache_tokens`
   占比与 cost 的实际 delta。离线分析脚本放 `scripts/`。
2. **召回侧**：新建 `tests/test_compression_recall.py`——植入事实（planted fact），跑压缩，
   量化压缩前后召回率。做出 nanobot 自己的「92% vs 58%」。
3. **governance decay**：新建测试钉住「压缩后系统约束/角色设定是否仍在上下文里」。
   先读 `history/prompt_builder.py` 确认系统提示是否被排除在压缩范围外——如果没有，
   这是个安静的安全问题。
4. **对照替代路线**：`history/tool_pruning.py` 是截断路线，`history/message_converter.py:100-119`
   已有 char budget。业界称输出截断降本 38% 且不重写前缀。把两条路线的成本/召回曲线并排比。

**产出**：`docs/compression-vs-cache-2026-09.md`。据此决定要不要动 `history_settings.py`
的默认值（`compress_ratio: 0.8`、`compress_max_summary_tokens: 600`）。

### 观望，本轮不做 ⚪

- **A2A / ACP**：mailbox 是进程内、不跨组织的，现在接 A2A 收益为零。
- **agent 支付**：无场景。
- **框架抽象层**：业界建议「别押注单一框架」——nanobot 本身就是框架，不适用。
- **Agent Skills L3 深化**：L1/L2 已够用，手动 `read_file` 没造成实际痛点。

---

## 四、验证

| 批次 | 验证方式 |
|---|---|
| 全部 | 每个逻辑单元 checkpoint 提交（AGENTS.md #6）；提交前 `python3 -m pytest tests/ -q` 全绿 |
| A | 新 `tests/test_mcp_client.py` 通过；真连本机两个 MCP server 成功注册工具；旧版 server 仍能连 |
| B | 起本地 OTLP collector，确认 workflow→agent→tool→model 完整 span 树；mod 关闭时零开销、零依赖导入 |
| C | 造超预算场景，确认护栏触发且 `RoundLifecycle` 状态转换未被破坏 |
| D | 产出数据文件，代码零改动。测试本身进 CI |

网关重启纪律：`systemctl restart nanobot-gateway` **禁止在 agent 活动时执行**
（`gateway.log` 近 5 分钟有 Broadcast/litellm/MailboxHub 行即算活动）。

**排期**：A 与 D 可并行（一个动 MCP、一个只读分析，无冲突）。B 必须在 C 之前
（C 依赖 B 的 `llm:response` 事件）。

---

## 五、前提与免责

- 表格里标「二手」的信源多为 SEO 博客，采用率百分比与降本数字（857x、70-90%、38%）
  **方向可信、具体数值不可引用**。只有 MCP 规范那条是一手信源，也只有那条有硬性时间窗口。
- 批次 D 的业界数据（92%→58%）来自别人的生产系统，nanobot 的数字可能完全不同——
  所以第 2 步是自己测，不是照搬结论。
- 本计划全程不碰架构线（`docs/plan-2026-09-07-arch-refactor.md`）的 Phase 1/2/3 范围。唯一交叠点是批次 B 要往 `events.py`
  加两个事件——那是新增，不改现有状态流。

---

## 来源

一手：
- [The 2026-07-28 Specification | MCP Blog](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [The 2026 MCP Roadmap | MCP Blog](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/)

二手：
- [AI Agent Trends H2 2026: 7 Shifts for Builders](https://www.betterclaw.io/blog/ai-agent-trends-h2-2026)
- [Context Engineering in 2026: Why We Stopped Compacting Our Agent's Context](https://www.louisbouchard.ai/context-engineering-2026/)
- [How OpenTelemetry Traces LLM Calls, Agent Reasoning, and MCP Tools | Greptime](https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions)
- [OpenTelemetry GenAI Semantic Conventions: Tracing AI Agents in Production (2026)](https://veraexmachina.com/tech/opentelemetry-genai-agent-observability-production/)
- [Agent Skills Explained: How SKILL.md Files Work](https://www.firecrawl.dev/blog/agent-skills)
- [How to Build an Evaluation Harness for AI Agents Before Production](https://digitalthoughtdisruption.com/2026/07/31/ai-agent-evaluation-harness/)
- [Governance Decay: How Context Compaction Silently Erases Safety Constraints (arXiv)](https://arxiv.org/pdf/2606.22528)
- [Beyond Compaction: Structured Context Eviction for Long-Horizon Agents (arXiv)](https://arxiv.org/pdf/2606.11213)
