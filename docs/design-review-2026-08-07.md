# nanobot-src 设计评审(概览版)

> 日期:2026-08-07 · 目的:通读全代码,记录结构/设计问题/扩展点,作为日后"叠加新功能"思考的底稿。
> 范围:`/root/nanobot-src/nanobot/`(约 34,961 行 python)。非改动清单,是观察与建议。

---

## 0. TL;DR(一句话结论)

**架构骨架是好的**(BaseChannel ABC + MessageBus + orchestra/history 分层 + model_match 路由),但**数据落盘层没有收敛**:
`providers_models.json` 被 5+ 模块各自手写 load/save,agent `config.json` 在 callbacks.py 里散落 ~25 处写点,
`callbacks.py` 是一个 3000 行巨石。**新功能(无头 add/edit/del agent/provider/model/group)之所以"没处放、只能再抄一遍",根因就是这里没有单一数据服务层。**

---

## 1. 架构地图(它怎么搭起来的)

```
gateway (cli/commands.py: ~785)                 ← 程序总装:config→provider→engine→channels→bus
  ├─ MessageBus (bus/queue.py, events.py)       ← 入/出站消息收发(队列)
  ├─ provider  (providers/: base/litellm/httpx/registry/model_match…)
  ├─ GroupChatEngine (groupchat/orchestra/engine.py: 1681)   ← 群聊运行时大脑
  │    ├─ broadcast.py (1762)  开一轮广播,驱 agents
  │    ├─ mailbox.py (895)     邮箱/等待/打断机制
  │    ├─ chatroom_tools.py (1409) / tool_loop.py (863) / run_loop.py (171) / events.py
  │    └─ history/ : prompt_builder(874), persistence(182), agent_loader(271), context…
  ├─ channels (11 个): telegram/feishu/mochat/matrix/dingtalk/email/discord/slack/wecom/whatsapp/qq
  │    └─ base.py (139)  ABC: start/stop/send/_handle_message/is_allowed
  └─ 支撑: config/(283), cron/(440), heartbeat/, session/(242), tools/(memory_palace 605, filesystem, web…),
           skills/(loader 523 + settings/cron/debug 等 skill + scripts), security/
```

**做得好、值得保留的设计:**
- `BaseChannel`(ABC)统一了 11 个平台的接驳契约;`ChannelManager._dispatch_outbound` 用 `_progress`/`_tool_hint` 元数据做发送门控。
- `groupchat` 分层成 `orchestra`(运行时:engine/broadcast/mailbox)vs `history`(状态/提示/persistence),职责边界清晰。
- `providers/model_match.py`(sanitize→_family→resolve_provider 四段)是"数据自愈 + 路由"的正确范例——**这正是应该在别的数据上也复用的模式**。

---

## 2. 已确认的设计问题(量化、有据)

### A. `channels/telegram/callbacks.py` —— 3000 行巨石
- 2997 行,一个 `CallbacksMixin`,内含全部 inline-keyboard 事件流(`em_`agent / `pm_`provider/model / `ep_` / `ef:`)。
- `_on_callback` 从 L92 一个巨大 `if/callback_data.startswith(...)` 网开始,到 2915 行 —— 单方法巨大。
- **副作用:** agent `config.json` 写点在这文件里 ~25 处,`_save_pm`(写 providers_models)**8 处**。行为散落,必然漂移。

### B. `providers_models.json` 访问碎片化 —— 5+ 模块、4+ 套独立 load 实现
同一份文件,被以下各自实现读写:
1. `providers/litellm_provider.py` —— 运行时路由,自带 `_pm_path`
2. `providers/httpx_provider.py` —— **又一份 `_load_pm()`**(和 litellm 逻辑平行重复)
3. `channels/telegram/commands/providers.py` —— `_load_pm`/`_save_pm`(含 sanitize 自愈)
4. `channels/telegram/callbacks.py` —— 8 处 `_save_pm`
5. `skills/settings/scripts/settings_cli.py` —— **第三份 `_load`/`_save`/`_pm_path`**

→ 改文件结构/schema,要同步 5 处;模块间无单一入口。**这是"加功能要改多处"的最典型病灶。**

### C. agent `config.json` 写点散落 —— 无单一 agent 写入服务
- `~/.nanobot/agents/<name>/config.json` 在 callbacks.py 内 ~25 处写,外加 engine/chatroom_tools 各 1 处。
- 创建/modify agent 的字段语义(rank/effort/tools/prompt/model)由各处各自拼 dict,易产生键漂移(历史上有 `em_manual` 分支误删之类的先例)。

### D. 入站路径双轨 —— "死队列"注释是妥协
- telegram `message_handler._on_message` 对普通消息**直接 `engine.inject()`**,注释明写"publish_inbound 会撞上 dead consumerless queue";而命令路径走 `_handle_message → bus.publish_inbound`。
- 即:群聊入站绕开 bus,命令入站走 bus → 两条平行路由,心智负担 + 队列语义不清。

### E. 已有第三个设置消费端 —— 头无 admin 其实有种子
- `skills/settings/scripts/settings_cli.py` 已经实现 `providers list/add/edit/remove` + `models list/add/remove`,是"无头管理"的雏形。
- 但它**复刻**了 pm 的 `_load/_save`,没接 sanitize,没接 Telegram 那套。
- 结论:**"无头 add/edit/del"不是新空地,而是第四个要写同一套 CRUD 的地方。**

### F. 次要观察
- 11 个 channel 只有 telegram 深接群聊;其余走 bus,群聊能力大概率未接通或行为不一(待逐个核实)。
- `cli/commands.py` 的 `gateway` 是 ~160 行总装函数,所有依赖硬绑进去,难单独测试。
- `logs/`(POWERSHELL):gateway 日志 INFO 级看不到消息路径,调试要开 -v(已知痛点,见 nanobot-debug 技能)。

---

## 3. 建议的方向(为了"往上叠新功能")

按收益/风险排:

### ① 抽一个"设置存储单一入口"服务层(最高优先,直接解 B/C/E)
新增一模块(如 `nanobot/state/settings_store.py` 或 `providers/store.py`),收拢:
```
- providers_models.json:  atomically  load()→sanitize  /  save(data)   (唯一读写点)
- agents/<name>/config.json:  load_agent(name)  /  save_agent(name, cfg)  /  delete_agent(name)
- 分组/活动名单/leader:  已部分在 engine,如需要也归口
```
让 litellm/httpx/callbacks/providers.py/**settings_cli.py/未来的 headless admin** 全部**委托**它。
→ 新功能只改 store.py 一处;数据 schema 变更一处同步;自愈(sanitize)天然集中。
**这不要求"重构 Telegram 回调"就能落地**——先建层,各消费端按需切换,零回归风险。

### ② 把"无头 admin"做成 store.py 之上的一等 CLI
- 在 ① 之上,做一个通用管理 CLI(agent/provider/model/group 的 add/edit/del/list),
  让 `headless_drive.py`、`settings 技能`、甚至未来 Telegram 命令都只是它的薄壳。
- 复用:`engine.add/remove/delete_agent/save_group/load_groups/delete_group` + store.py 的 provider/model。
- **这正是你之前问的"无头 add/edit/del"的正规落点**——不是第四个复制,而是唯一实现。

### ③ 拆 `callbacks.py`(中期,收益大但要动生产)
按域拆成 `telegram/callbacks/` 子包:`agent_cb.py` / `provider_cb.py` / `group_cb.py` / `chat_cb.py` / `history_cb.py`,
每个只做"inline-keyboard → store.py / engine"的薄适配。`_on_callback` 改成按前缀分发到子处理器。
→ 每职责一文件,新增命令/回调只加一个小文件。

### ④ 统一入站路由(中期)
把"无头/命令"两条入站路径收成一条:群聊也走一个 `InboundRouter`(普通消息→engine.inject,命令→bus),
消除"dead consumerless queue"妥协,其它 channel 也能复用群聊。

### ⑤ provider 路由收敛(顺手)
`httpx_provider._load_pm/_resolve_provider` 改用 `model_match.resolve_provider`,消除与 litellm 的平行重复。

---

## 4. 可叠加新功能的扩展点(顺势而为)

- **agent** 加字段(rank/effort/tools/workspace_scope)→ 改 store.py + agent_loader;telegram/headless 自动拿到。
- **provider/model** 加 schema(自定义 header/retryDelays/flattenTools)→ 改 store.py schema,四处消费端同步。
- **group** 加"分组快照/批量载入"→ engine 上已有 save/load/delete 基础。
- **新 channel** 接群聊 → 触 ④(统一路由)后,其它平台直接复用,不用复制 telegram 的注入 hack。

---

## 5. 待思考(下次再议,记在这)

1. 单一 store 层放哪个包合适?`providers/`(离路由近)vs 新 `state/`(离数据近)——命名之争先放着。
2. `callbacks.py` 拆文件要不要顺带改回调数据结构(字符串前缀 → 带参数枚举),避免 8 处 `pm:...` 魔数。
3. settings 技能 CLI 与未来的 headless admin CLI 是否合并成一个;还是保留两个薄壳共享 store。
4. 是否给 store.py 加"变更回调/事件"(保存后通知 engine/正在跑的广播刷新),让改动即时生效而非下次重启。

---

## 6. 前车之鉴:`refactor/phase3-split-giants`(别重蹈)

**有一条 refactor 分支做过同款重构但翻车**,读它学教训:
- **好计划**:`PLAN.md` 诊断和我审查完全一致(D2 私有属性横穿 / D6 巨型文件 / D7 存储散乱 / D8 入站双轨),且 P0-P2 真做成了(引擎公共 API、ChatUI 抽象)。
- **错在执行**:一条分支领先 stable 106 提交、末提交 `+551k 行/2127 文件`;删 `SessionManager`、迁移 `GroupChatState` 存储路径(P5.1/P5.2,带 `[待确认]` 就做);stash 里有 `pre-rollback` → 最终没合回,停摆。
- **教训 → 本文件后续做法**:小步、可合并、向后兼容、不删模块、不迁存储、每步带测试。
- 分支上已有的 P1(公共 API)/P2(ChatUI)是**值得捡的遗产**,但不影响本文件前几节;若要接可单独摘。

## 7. 已落地(小步,2026-08-07)

- **`nanobot/state/settings_store.py`**(新):`providers_models.json` + `agents/<name>/config.json` 的**单一读写点**(load/save 含 sanitize 自愈;provider/model/agent 的 list/add/update/delete)。纯函数、可测、向后兼容、不改任何现有路径。
- **`scripts/headless_drive.py` 加 `admin` 子命令**:`admin agents|providers|models|groups` 的 add/edit/rm/list,薄壳调 store + engine(active/group)。实测写回环无残留,`tests/test_settings_store.py` 6 过。
- **踩到的坑(取值记)**:① sanitize 会清 `len<5` 的"模型 id"——写进 store 的 id 必须真实形态;② agent 要能被 `_scan_agents_dir` 收录,**目录里必须有 `SOUL.md`(人设)**——光写 `config.json` 的 agent 是隐形的,`engine.delete_agent` 也找不到;`store.create_agent` 已一并写 SOUL.md。
- **未做(明确圈外)**:未拆 callbacks、未接 Telegram 回调去重、未迁存储路径、未删任何模块。—— 下步可选:把 settings 技能 CLI / litellm / httpx 的 `providers_models.json` 读取迁到 `store.load_pm()`。

（完）