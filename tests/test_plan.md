# Nanobot 测试计划 — Agent / Provider CRUD / 并发 / 安全

> 目标：覆盖 Agent CRUD、Provider CRUD、Model CRUD、持久化、并发边界、安全审计、边界用例七个维度。
> 优先级：P0=必须修+测（阻断/泄露） | P1=应修+测（健壮性） | P2=可选（体验/性能）
> 被测代码：`nanobot/groupchat/orchestra/engine.py`、`nanobot/groupchat/history/persistence.py`、`nanobot/skills/settings/scripts/settings_cli.py`

---

## 1. Agent CRUD（engine.py）

被测接口：`add_agent(name)` / `remove_agent(name)` / `delete_agent(name)` / `reorder_agents(new_order)` / `set_leader(name)` / `active_agents` / `registry`

### 1.1 添加
| # | 用例 | 前置 | 步骤 | 预期 | 优先级 |
|---|------|------|------|------|--------|
| A1 | 正常添加 | registry 含 3 agent，active 空 | `add_agent("Harper")` | 返回成功消息；active=[Harper]；save_active 落盘 | P0 |
| A2 | 大小写不敏感 | registry 含 "harper" | `add_agent("HARPER")` | 解析为 harper 并加入，无重复 | P0 |
| A3 | 重复添加 | active=[Harper] | `add_agent("Harper")` | 返回"已在对话中"，active 不变 | P0 |
| A4 | 不存在的 agent | — | `add_agent("Ghost")` | 返回"不存在"+可用列表，active 不变 | P0 |
| A5 | 空名/None | — | `add_agent("")` / `add_agent(None)` | 不崩溃，返回错误消息 | P1 |
| A6 | 特殊字符/XSS | — | `add_agent("<script>alert(1)</script>")` | 按不存在处理，不注入 | P1 |

### 1.2 删除
| # | 用例 | 前置 | 步骤 | 预期 | 优先级 |
|---|------|------|------|------|--------|
| A7 | 正常删除 | active=[A