# nanobot 架构地图

## 0. 总原则（优先于包名）

1. **`History` 是唯一处理上下文的逻辑层**  
   所有 durable 对话数据的写入、压缩、裁剪、按 agent 投影（`build_for_*`）
   都在 `nanobot.core.history.History`（及它调用的纯函数）内完成。

2. **其它类只承载群聊 / 协作 / 调度逻辑**  
   `groupchat.runtime`：谁参与、何时跑、打断、mailbox、tool_loop、cycle。  
   它们可以 *调用* History，但不得维护第二份长期 messages 真相。

3. **视图与数据分离**  
   `groupchat.display` 只把事件变成用户可见文本；不写 History，不 import runtime。  
   需要副作用时由 runtime **注入回调**。

4. **WorkingMemory ≠ 上下文**  
   只是单 agent、单 cycle 的 tool 协议缓冲；`refresh` 必须从 History 投影重建。

```text
             ┌──────────────────────────┐
             │  display（视图）          │
             │  格式化 / 流式 / 面板     │
             └────────────▲─────────────┘
                          │ 只读渲染调用
┌─────────────────────────┴─────────────────────────┐
│  runtime（协作）                                    │
│  engine · broadcast · agent_cycle · mailbox · …   │
│  WorkingMemory（ephemeral tool buffer）             │
└─────────────────────────┬─────────────────────────┘
                          │ commit_turn / build 投影
             ┌────────────▼─────────────┐
             │  History（上下文逻辑·唯一） │
             │  fragments · compress ·   │
             │  build_for_groupchat · …  │
             └──────────────────────────┘
```

## 1. 仓库布局

```text
nanobot-src/
  nanobot/
    core/history.py     # 唯一上下文逻辑层
    groupchat/
      runtime/          # 协作 / 群聊
      context/          # History 投影与策略（非第二 store）
      display/          # 视图
    gateway/            # 入站网关（非群聊中间层）
  tests/
  docs/
```

| 勿混淆 | |
|--------|--|
| `History` | 上下文数据 + 处理 |
| `groupchat.context` | prompt/rank/settings **投影**，不拥有 transcript |
| `WorkingMemory` | ephemeral tool buffer |
| `nanobot.gateway` vs `groupchat.runtime` | 网关 vs 群聊协作 |

运行时数据：`~/.nanobot`。

## 2. 写入路径（唯一）

```text
agent/user 产出
    → History.commit_turn / add_from_sender
    → engine._persist_after_history_write   # 可选 I/O
```

`commit_agent_turn`（runtime 辅助）只做：拼 tool_log 字符串 + 调用 History。

## 3. 读取 / 投影

- `PromptBuilder.build_*`：读 History + 配置 → LLM messages  
- `History.build_for_groupchat` / `build_for_llm`：History 内投影  
- display：读事件/字符串 → UI（无 History 写）

## 4. runtime 模块（协作）

| 文件 | 角色 |
|------|------|
| `engine.py` | 注册表、持有 `history`、persist hook |
| `broadcast.py` | round setup / launch / gather |
| `agent_cycle.py` | per-agent cycle（调用 History commit + WM refresh） |
| `mailbox.py` / `collab_bus.py` | 协作投递总线（`CollabBus`）；`round_log` ≠ History |
| `working_memory.py` | ephemeral tool buffer + `commit_agent_turn` |
| `cycle_controller.py` | cycle 分支决策（仍可 shadow） |

## 5. display 模块（视图）

| 文件 | 角色 |
|------|------|
| `display.py` | 文案 / icon / banner |
| `streaming.py` | 流式编辑 |
| `broadcast_view.py` | 群面板；副作用用回调 |

守卫：`tests/test_display_no_runtime_import.py`、`tests/test_layer_boundaries.py`。

## 6. 演进

- [x] orchestra/history shim 删除；gateway 改名  
- [x] `agent_cycle` 抽出  
- [x] History.commit_turn + persist 钩子分离  
- [ ] CycleController shadow → 权威  
- [x] 消息投递收口（`CollabBus` + `round_log` 命名；≠ History）
- [ ] 更多 call-site 只通过 `HistoryConversation` / `commit_turn`  
- [ ] display.visibility 去掉 ranks 策略 re-export  

## 7. 旧文档

`groupchat-coupling-fix.md` 等路径名可能仍写 orchestra；以本文与 `AGENTS.md` 为准。

## 8. Telegram：设置面板 vs 对话显示

| 路径 | 用途 |
|------|------|
| `channels/telegram/settings_history_panel.py` | **/history 设置面板**（改 context 旋钮） |
| `channels/telegram/commands/settings.py` + `callbacks/*` | 设置命令与按钮回调 |
| `groupchat/display/*` | **对话过程** UI（流式、BroadcastView、status_tracker） |

二者分离：设置不走 BroadcastView；对话不走 settings_history_panel。

### Collab delivery vs History

- `CollabBus.round_log`：本轮投递索引（list/quote），`start_round`/`clear` 清空。
- `History`：唯一 durable 对话上下文；`chatroom_send` 不直接 commit。
- Durable 写入：`commit_agent_turn` → `History.commit_turn`。
