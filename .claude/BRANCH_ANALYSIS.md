# Nanobot 分支清理分析报告

生成时间: 2026-09-07

## 分支总览

| 分支类型 | 数量 | 状态 |
|---------|------|------|
| 本地分支 | 11 | 需要清理 |
| 远程分支 | 21 | 含8个stash临时分支 |
| 未合并到main | 7 | 需要决策 |

---

## 分支关系图

```
main (2026-07-06) ─────────────────────────────────────────────────────┐
│                                                                       │
├─→ fix/restore-protections-and-config ──(已合并)──→ feat/ui-redesign  │
│                                                                       │
├─→ stable-20260803-fixes (2026-08-08)                                │
│   │   └─→ feat/ui-redesign (当前分支, 2026-09-04) ←── 推荐          │
│   │       包含stable全部改动 + 45个新commit                          │
│   │                                                                   │
│   └─→ fix/groupchat-headless-stable-align (2026-07-20)              │
│       ⚠️ 176个独有commit，与ui-redesign有分歧                        │
│                                                                       │
├─→ fix/openrouter-model-not-found (2026-07-20)                       │
│   9个独有commit: hot reload + broadcast重构                          │
│                                                                       │
├─→ fix/hotreload-20260517 (2026-07-22)                               │
│   19个独有commit: 可能是openrouter的前序版本                          │
│                                                                       │
├─→ refactor/phase3-split-giants (2026-07-27)                          │
│   ⚠️ 超大改动(553k+ lines)，已停滞                                    │
│                                                                       │
├─→ agent/codex & agent/nanobot (2026-06-24)                          │
│   添加webui前端(React+Vite+Tailwind)，未被其他分支包含               │
│                                                                       │
└─→ test/perf-baseline (2026-04-03)                                   │
    已过时，可删除                                                     │
```

---

## 详细分支分析

### 1. feat/ui-redesign ⭐ 当前分支，推荐保留

**状态**: 最新活跃开发

**包含改动**:
- UI重构：panel渲染器统一、callback路由注册
- Mods插件架构：bus subscribers、config-gated lifecycle
- Skills标准化：Agent Skills frontmatter compat
- 多项bugfix：synthesis幽灵打断、grace period计数、nudge死循环

**与main差异**: +12,623/-21,247 lines, 212 files

**建议**: ✅ 保留，作为下一个release的基础

---

### 2. stable-20260803-fixes

**状态**: feat/ui-redesign的直接祖先

**包含改动**:
- settings持久化收敛到settings_store
- headless CLI支持
- 多项orchestra bugfix

**与main差异**: +14,890/-20,869 lines

**关系**:
```
git merge-base --is-ancestor stable-20260803-fixes feat/ui-redesign
# YES
```

**建议**: ✅ 合并到main后可删除（已被ui-redesign完全包含）

---

### 3. fix/groupchat-headless-stable-align ⚠️ 重要分歧分支

**状态**: 独立重构方向，176个独有commit

**核心改动**:
```
nanobot/context/ranks.py           +120 lines (新增)
nanobot/context/repetition.py      +108 lines (新增)
nanobot/channels/telegram/callbacks.py → 拆分为多个模块
  - cb_agents.py    +796
  - cb_logs.py      +588
  - cb_prompts.py   +292
  - edit.py         +537
  - param_docs.py   +486
```

**重构内容**:
- History API改进：`last_content_by_sender`，`estimate_tokens`
- CycleController权威化：`cycle_gate`
- CollabBus交付端口重构
- 死代码删除：callbacks.py从3290行拆分成多模块

**建议**: ⚠️ 需要决策
- **选项A**: 从中提取有价值的重构（ranks.py, repetition.py, callbacks拆分）
- **选项B**: 在ui-redesign基础上重新实现相同架构改进

---

### 4. refactor/phase3-split-giants ❌ 建议废弃

**状态**: 已停滞，改动过大

**改动统计**:
- +553,311/-1,368 lines (不含测试数据)
- 2143 files changed
- 包含大量.jsonl测试数据文件

**核心改动**:
```python
删除文件:
  nanobot/session/manager.py  (-242 lines)

重大修改:
  nanobot/groupchat/orchestra/broadcast.py  +381 lines
  nanobot/groupchat/orchestra/request_log.py +150 lines
  nanobot/groupchat/orchestra/status_tracker.py +166 lines
```

**问题**:
- 改动规模过大，难以review
- 包含测试数据污染
- 最后更新2026-07-27，已停滞1.5个月

**建议**: ❌ 废弃此分支，如果有价值改动可手动提取

---

### 5. agent/codex & agent/nanobot ⚠️ 待决策

**状态**: 添加webui前端

**内容**:
- React 18 + Vite + Tailwind CSS
- Radix UI组件库
- lucide-react图标
- i18next国际化
- react-markdown + KaTeX数学公式

**文件数**: 144个webui文件

**建议**: ⚠️ 需要决策
- **如果要webui**: 需要将webui目录合并到当前分支
- **如果不要webui**: 可删除此分支

---

### 6. fix/openrouter-model-not-found

**状态**: 有价值改动

**独有commits** (9个):
```
feat(config): on-demand hot reload for providers + agents.defaults
fix(mailbox): break out of wait() when all agents idle
fix(orchestration): resolve message duplication and state consistency
fix(tool): resolve force_no_tools bug
feat(groupchat): variable-driven state and context control
fix(prompts): remove all wait() references
simplify(broadcast): remove timeout/sentinel/wait
fix(restart): use os.execv for reliable restart
refactor(broadcast): structural overhaul
```

**建议**: ✅ 选择性合并hot reload相关改动

---

### 7. fix/hotreload-20260517

**状态**: 可能是openrouter的前序版本

**独有commits**: 19个

**建议**: ⚠️ 与fix/openrouter对比后决定保留哪个

---

### 8. test/perf-baseline

**状态**: 已过时 (2026-04-03)

**建议**: ❌ 删除

---

### 9. 远程 stash/* 分支 ❌ 全部删除

```
nanobots/stash/detached-phase2-wip
nanobots/stash/dev-tool-logs-wip
nanobots/stash/dev-user-interrupt-wip
nanobots/stash/groupchat-headless-wip
nanobots/stash/groupchat-opt-20260403
nanobots/stash/hotreload-wip
nanobots/stash/main-rank-corruption-wip
nanobots/stash/pre-rollback-stable-20260527
```

**性质**: 全部是临时保存点

**建议**: ❌ 安全删除

---

## 清理执行计划

### Phase 1: 安全删除（无风险）

```bash
# 1. 删除远程stash分支
git push nanobots --delete stash/detached-phase2-wip
git push nanobots --delete stash/dev-tool-logs-wip
git push nanobots --delete stash/dev-user-interrupt-wip
git push nanobots --delete stash/groupchat-headless-wip
git push nanobots --delete stash/groupchat-opt-20260403
git push nanobots --delete stash/hotreload-wip
git push nanobots --delete stash/main-rank-corruption-wip
git push nanobots --delete stash/pre-rollback-stable-20260527

# 2. 删除过时分支
git branch -D test/perf-baseline
git push nanobots --delete test/perf-baseline

# 3. 清理已合并分支（本地）
git branch -D fix/restore-protections-and-config
```

---

### Phase 2: 合并有价值改动（需要review）

**从 fix/groupchat-headless-stable-align 提取**:
```bash
# 选择性cherry-pick核心模块
git cherry-pick <commit> -- nanobot/context/ranks.py
git cherry-pick <commit> -- nanobot/context/repetition.py
# 或手动提取History API改进
```

**从 fix/openrouter-model-not-found 提取**:
```bash
# hot reload功能
git cherry-pick <commit-hash-of-hotreload-feat>
```

---

### Phase 3: Webui决策

**如果要保留webui**:
```bash
# 方案A: 直接合并agent/codex的webui
git merge agent/codex --no-commit
# 手动解决冲突，只保留webui目录

# 方案B: 提取webui到独立分支后合并
```

**如果不要webui**:
```bash
git branch -D agent/codex agent/nanobot
git push nanobots --delete agent/codex agent/nanobot
```

---

### Phase 4: 主线整合

**方案A（推荐）: feat/ui-redesign → main**
```bash
# 1. 确保ui-redesign测试通过
pytest

# 2. 合并到main
git checkout main
git merge feat/ui-redesign

# 3. 删除已合并分支
git branch -d stable-20260803-fixes
git push nanobots --delete stable-20260803-fixes
```

**方案B: 创建新release分支**
```bash
git checkout feat/ui-redesign
git checkout -b release/v0.3.0
# 以此为稳定版本
```

---

## 待决策问题

### 问题1: fix/groupchat-headless-stable-align如何处理？

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. 选择性合并 | 保留有价值重构 | 需要仔细review |
| B. 在ui-redesign重新实现 | 干净的历史 | 重复工作 |
| C. 暂时保留分支 | 不丢失工作 | 分支继续mess |

**建议**: 选项A - 提取 ranks.py, repetition.py, callbacks拆分结构

---

### 问题2: webui是否需要？

| 选项 | 理由 |
|------|------|
| 需要 | 有独立Web界面，用户体验更好 |
| 不需要 | 项目定位是Telegram Bot，webui维护成本高 |

---

### 问题3: refactor/phase3-split-giants有价值吗？

**可能的价值**:
- SessionManager删除（简化架构）
- broadcast.py重构

**建议**: 废弃此分支，如有需要可在稳定版本上重新规划Phase 3重构

---

## 分支清理优先级

| 优先级 | 操作 | 风险 |
|--------|------|------|
| P0 | 删除远程stash/*分支 | 无 |
| P0 | 删除test/perf-baseline | 无 |
| P1 | 合并stable到main | 低 |
| P1 | 删除fix/restore-protections | 无（已合并） |
| P2 | 提取fix/groupchat-headless改动 | 中（需review） |
| P2 | 提取fix/openrouter改动 | 中 |
| P3 | 决策webui去留 | 取决于产品规划 |
| P4 | 决策refactor/phase3 | 高（可能废弃） |

---

## 建议的下一步

1. **立即执行**: Phase 1安全删除
2. **本周完成**: Phase 2选择性合并
3. **产品决策**: webui去留
4. **版本规划**: feat/ui-redesign作为下一个release基础
