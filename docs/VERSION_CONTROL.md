# Nanobot 版本控制规范

> 生效日期：2026-09-01 | 状态：v1

---

## 1. 分支现状图

```
传统主干: main (停滞 2 个月)
  └─ 已删除 callbacks.py → 拆为 callbacks/ 包 (11 模块)

事实主线: fix/groupchat-headless-stable-align (176 commits ahead of ui-redesign)
  └─ 继承 main 的 callbacks/ 包架构

运行分支: feat/ui-redesign (HEAD: 8204a7c50)
  └─ 依赖 callbacks.py (829 行单体文件)
  └─ 对事实主线 ahead 39 / behind 176
  └─ 架构分叉: merge/rebase 不可行 (85 文件冲突)
  └─ 包含 429 限流、edit-flow 泄漏等高频痛点修复

其他分支 (已全推远程):
  agent/codex, agent/nanobot, fix/hotreload-20260517,
  fix/openrouter-model-not-found, fix/restore-protections-and-config,
  refactor/phase3-split-giants, stable-20260803-fixes, test/perf-baseline
```

### 根因

`feat/ui-redesign` 分叉点 `f0a51db4a`（05-23）。此后主线删除了 `callbacks.py` 并重构为 `callbacks/` 包（11 模块），而 ui-redesign 持续在旧架构上开发。两条线在 `callbacks` 层的架构冲突导致 85 文件无法自动合并。

---

## 2. 分支策略

### 2.1 主分支认定

**承认 `fix/groupchat-headless-stable-align` 为事实主线（`main` 的继承者）。**

理由：
- 包含 `main` 全部提交 + 74 个额外提交
- 采用 `callbacks/` 包架构（模块化，可维护）
- 是唯一能承载后续开发的基线

### 2.2 分支命名规范

| 前缀 | 用途 | 基线 |
|---|---|---|
| `feat/*` | 新功能开发 | 事实主线 |
| `fix/*` | 缺陷修复 | 事实主线 |
| `refactor/*` | 重构 | 事实主线 |
| `release/*` | 发布候选 | 事实主线 tag |
| `stable/*` | 长期稳定分支 | 事实主线 tag |
| `experimental/*` | 实验性架构变更（如 ui-redesign 类） | 事实主线 |

### 2.3 运行分支规则

- 生产运行分支可以是任意分支（当前为 `feat/ui-redesign`）
- 运行分支必须打 `running-YYYYMMDD-HHMM` tag
- 运行分支与事实主线的架构差异必须文档化（见第 5 节）

---

## 3. 修复流转规则

### 3.1 卡死级修复（P0/P1）

```
发现修复 → cherry-pick 到运行分支 → 验证 → cherry-pick 到事实主线
```

**流程**：

1. 在事实主线提交修复（基线：`fix/groupchat-headless-stable-align`）
2. `cherry-pick <commit> --onto feat/ui-redesign` 到运行分支
3. 运行分支验证通过后，标记 `running-*` tag
4. 同一 commit 再 cherry-pick 回事实主线（如已在主线则跳过）

### 3.2 冲突处理原则

**加法冲突（两边各自加功能）→ 保留两边，机械合并**

判定标准：冲突块中两边代码无语义矛盾，各自解决不同问题。
示例：`run_loop.py` 中 ui-redesign 的 `_summary_requested` 块与 main 的 `_running` 复活块 → 并存。

**减法冲突（一边删一边改）→ 需人工确认语义**

判定标准：一方删除了另一方修改的代码。需理解删除意图后再合并。

### 3.3 架构分叉修复的长期方案

`feat/ui-redesign` 独有的 `_send_panel` 方法（20 行）应移植到主线 `callbacks/helpers.py`，然后逐步替换主线的 106 处 `edit_message_text` 调用点。移植完成后，`feat/ui-redesign` 可废弃。

---

## 4. Tag 规范

### 4.1 Tag 类型

| Tag 模式 | 触发时机 | 谁打 |
|---|---|---|
| `running-YYYYMMDD-HHMM` | 每次运行分支更新 | 执行 cherry-pick 者 |
| `stable-YYYYMMDD-<desc>` | 里程碑稳定版本 | 团队协商 |
| `v<major>.<minor>.<patch>` | 正式发布 | Leader |

### 4.2 当前有效 Tag

| Tag | HEAD | 说明 |
|---|---|---|
| `running-20260901-1333` | `8204a7c50` | 当前运行版本 |
| `stable-20260901-ui-redesign` | `d796260ef` | ui-redesign 稳定基线 |

### 4.3 Tag 规则

- `running-*` tag 必须指向运行分支的当前 HEAD
- 更新运行分支后，旧 `running-*` tag 保留（可追溯）
- `stable-*` tag 不可移动（immutable）

---

## 5. 重启与回滚

### 5.1 关键认知

**Python 已加载旧代码到内存，修改磁盘文件不影响当前运行进程。**
重启 `nanobot-gateway.service` 后才生效。

### 5.2 重启前验证

```bash
# 1. 确认工作区干净
cd /root/nanobot-src && git status --short

# 2. 确认 HEAD 是预期 commit
git log --oneline -1

# 3. 语法检查（关键修改文件）
python -m py_compile nanobot/groupchat/orchestra/broadcast.py
python -m py_compile nanobot/groupchat/orchestra/tools/tool_loop.py
# ... 其他修改的文件

# 4. 运行测试（如可用）
python -m pytest tests/ -x -q --timeout=60
```

### 5.3 重启命令

```bash
sudo systemctl restart nanobot-gateway.service
sudo journalctl -u nanobot-gateway.service -n 50 --no-pager  # 检查启动日志
```

### 5.4 回滚流程

```bash
# 回滚到上一个 running tag
cd /root/nanobot-src
git checkout running-20260901-1333   # 或上一个 stable tag
sudo systemctl restart nanobot-gateway.service
```

### 5.5 回滚候选

| 回滚目标 | Tag | 说明 |
|---|---|---|
| 当前稳定基线 | `stable-20260901-ui-redesign` | 含在途改动提交 |
| 上一个运行版本 | `running-20260901-1333` | 当前运行版本自身 |
| 出厂基线 | `f0a51db4a` | 分叉点（05-23，极不推荐） |

---

## 6. 附录：已执行操作记录

| 操作 | 结果 |
|---|---|
| 11 分支全推远程 | ✅ 原远程仅 2 个 |
| tag `running-20260901-1333` | ✅ |
| tag `stable-20260901-ui-redesign` | ✅ |
| 在途改动提交 `d796260ef` | ✅ |
| cherry-pick `13ffb7eaa` (cron 死循环) | ✅ 零冲突 → `813a9d5d5` |
| cherry-pick `d2bf4cf92` (无限 nudge) | ✅ 1 处加法冲突已解 → `8204a7c50` |
| 待处理: `1e4307b75` (discussion-killing) | 5 处加法冲突待解 |
| 待处理: `e4a171a9a` (synthesis 幽灵打断) | 2 处加法冲突待解 |