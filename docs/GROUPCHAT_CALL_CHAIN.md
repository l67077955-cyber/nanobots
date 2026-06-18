# GroupChat 调用链（当前架构）

GroupChat 是多 Agent 协作核心。`agent/` 目录已废弃，入口为 `GroupChatEngine`。

## Gateway 启动

```
nanobot gateway
  └─ cli/commands.py::gateway()
       ├─ MessageBus()
       ├─ ChannelManager(config, bus)
       ├─ GroupChatEngine(...)          # 始终创建
       └─ TelegramChannel.set_groupchat_engine(gc_engine)
```

## Telegram 用户消息 → 回复

```
用户发文字
  └─ telegram/message_handler.py::_on_message()
       ├─ 下载媒体 / 转写语音
       ├─ agents.py::_ensure_gc_send(chat_id)   # 绑定 send/edit 回调到 Telegram
       └─ GroupChatEngine.inject(content)
            ├─ 0 个 active agent → 提示 /addagent
            ├─ 1 个 active agent → _start_group_loop() 或 _input_queue
            └─ 2+ 个 active agent → 群聊 broadcast 循环

GroupChatEngine._start_group_loop()
  └─ orchestra/broadcast.py  （多 Agent 轮转发言）

单 Agent 直聊（cron/heartbeat 也走此路径）
  └─ engine.direct_chat(msg)
       ├─ prompt_builder.build_agent_prompt()
       ├─ engine._chat_with_tools()
       │    └─ orchestra/tools/tool_loop.py::chat_with_tools()
       │         └─ nanobot/tools/registry.py::ToolRegistry  （工具实现层）
       └─ StreamingDisplay → Telegram edit_message 流式更新
```

## Telegram 按钮回调

```
CallbackQuery
  └─ callbacks/core.py::_on_callback()
       ├─ add:/rm:/edit: …     → GroupChatEngine.add/remove_agent
       ├─ ep_/pm_/em_ …        → providers_models 配置
       ├─ pre/pr* …            → PromptBuilder / manifest
       ├─ log/rlog* …          → 会话日志 UI
       ├─ hs_*                 → callbacks/history.py
       └─ think_*              → callbacks/think.py
```

## 模块职责

| 模块 | 职责 |
|------|------|
| `groupchat/orchestra/engine.py` | 核心：Agent 注册表、inject、direct_chat、工具循环入口 |
| `groupchat/orchestra/broadcast.py` | 多 Agent 群聊编排 |
| `groupchat/orchestra/mailbox.py` | Agent 间消息邮箱 |
| `groupchat/history/prompt_builder.py` | 多 Agent 人设 prompt 拼装 |
| `orchestra/tools/tool_loop.py` | LLM ↔ 工具迭代循环（调度层） |
| `nanobot/tools/*` | 工具实现（read_file、exec、web…） |
| `channels/telegram/*` | Telegram 适配（应收窄，不含业务编排） |
| `bus/queue.py` | 部分旁路（cron 出站）；主路径直连 engine |

## 工具层说明

- **不是两套重复实现**：`orchestra/tools/` 负责循环调度，`nanobot/tools/` 负责具体工具。
- 修改工具行为 → 改 `nanobot/tools/`；修改迭代/裁剪逻辑 → 改 `tool_loop.py`。