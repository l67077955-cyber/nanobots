# AGENTS.md — nanobot-src 项目级 Agent 约定

本文件是写给**在本仓库工作的所有 AI agent**的项目指令。动手前先读完。
规范格式参考 [agents.md 开放标准](https://agents.md/)（"给 agent 看的 README"）。

## 项目是什么

nanobot 是一个多 agent 群聊系统：Telegram 通道 + 群聊编排（orchestra）+
工具循环 + 历史压缩。**本仓库被多个 agent 并行实时编辑，且有正在运行的生产网关。**

## 环境与命令（改动后必须执行的验证）

```bash
# 1. 语法检查（每次编辑后）
python3 -m py_compile <你改的所有文件>

# 2. 全量测试（提交前必须全绿）
python3 -m pytest tests/ -q

# 3. 只跑相关子集（快速迭代时）
python3 -m pytest tests/test_mods.py tests/test_events.py -q
```

- 网关是 systemd 服务：`systemctl restart nanobot-gateway`。
  **禁止在 agent 活动（gateway.log 近 5 分钟有 Broadcast/litellm/MailboxHub 行）时重启。**
- 日志：`/root/.nanobot/logs/gateway.log`。

## 硬性红线（违反 = 你的改动会被回滚）

1. **先写测试，再修 bug**。没有钉住行为的回归测试，不许动实现代码。
   参考既有模式：`tests/test_user_ingress.py`（真对象 + 微小假件，不 mock 内部）。
2. **禁止最小 diff 止血式补丁堆积**。发现竞态/死循环时，先判断是不是
   状态无主导致的（见 `round_lifecycle.py` 的教训），修根源，不加第 N 个
   护栏 flag。历史上"只增不减"的补丁层（净增 3:1）就是这么来的。
3. **加行为 = 写 mod，不改核心**。新护栏/提醒/指标/策略一律走
   `~/.nanobot/mods/<名字>/mod.py`（见 `docs/MOD_PLUGIN_GUIDE.md` 和
   `nanobot/mods/builtin/` 示例）。核心文件只修核心 bug。
4. **删代码和加代码同等重要**。确认死代码（零引用、零生产者）就删，
   删除时在 commit message 里写明证据。参考 `ffc1ab8f7`。
5. **编辑前重读目标代码块**——其他 agent 可能刚改过它，历史行号不可信。
6. **每完成一个逻辑单元就 checkpoint 提交**，不要攒大 diff。
7. **修不动就停手汇报**，不要带着未验证的改动继续堆叠。

## 事件与 mod（新行为的唯一入口）

- 事件目录：`nanobot/groupchat/orchestra/events.py` 的 `EVENTS`。
- 订阅方式：继承 `nanobot.mods.base.Mod`，方法名 `on_<event 下划线化>`。
- 观察型（tier 1）随便写；过滤型（tier 2）只能往 payload 里的可变容器
  append，不许替换。

## 架构速览（改哪找哪）

```
nanobot/groupchat/orchestra/
  run_loop.py        # 会话主循环：消费用户消息 → 开轮次
  broadcast.py       # 每轮编排（大文件，改前先读 round_lifecycle）
  round_lifecycle.py # 轮次状态机（ACTIVE/WINDING_DOWN/ENDED）——唯一状态源
  user_ingress.py    # 用户消息唯一决策点（投递/排队/开轮）
  mailbox.py         # agent 间消息 + 打断配额 + 会话池
  events.py          # 事件总线（mod 订阅处）
  tools/tool_loop.py # LLM→工具循环（协作打断检查点）
  tools/chatroom_tools.py  # chatroom_send/wait/end_discussion 等工具
nanobot/mods/        # mod 系统（base/registry/manager/builtin）
```

已知的架构不变量（改动必须保持）：
- `RoundLifecycle` 是轮次状态唯一归属；旧 flag（`leader_end_event`、
  `engine._running`）由它的转换同步翻转。
- `UserIngress` 是 `engine._input_queue` 的唯一消费者决策点。
- `BroadcastView` 是纯渲染——**不许**在 display 层加控制流副作用
  （历史上的教训：视图里触发打断/扣积分，换视图即静默失效）。

## 完成定义（DoD）

- [ ] `py_compile` 全过
- [ ] `pytest tests/ -q` 全绿（含你新增的回归测试）
- [ ] commit message 写清 what + why + 证据（引用 issue/日志/测试）
- [ ] 涉及线上行为时：确认 agent idle → 重启网关 → 观察 gateway.log 首轮
