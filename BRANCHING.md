# nanobots 分支规范

本 fork（`l67077955-cyber/nanobots`）的分支策略与上游 [HKUDS/nanobot](https://github.com/HKUDS/nanobot) 的 `main`/`nightly` 模型不同，以此文档为准。

## 分支角色

| 分支 | 用途 | 稳定性 |
|------|------|--------|
| `stable-YYYYMMDD` | **生产稳定线**（当前：`stable-20260527`） | 可部署 |
| `dev` | 活跃开发线 | 可能不稳定 |
| `main` | 历史稳定线（已被 `stable-*` 超越） | 只读参考，勿直接部署 |
| `feat/*` | 短期特性实验 | 合并后删除 |
| `upstream-main` | 上游 `main` 快照锚点 | 只读，用于对比 |

## 工作流

```
feat/* ──PR──► dev ──验证通过──► stable-YYYYMMDD ──打 tag──► v-stable-YYYYMMDD
```

1. 日常开发在 `dev` 或短期 `feat/*` 分支
2. 验证通过后 **合并进当前 stable 分支**（不是反过来）
3. 重大基线变更时从 stable 切新分支，如 `stable-20260701`
4. 在 stable 上打 tag：`v-stable-YYYYMMDD`

**禁止：**
- 在 `dev` 和 `stable` 上并行实现同一功能（会产生合并冲突，如 ForgetTool）
- 在分支历史中留 `backup:` 类 commit（用 tag 代替）
- 直接向已落后的 `main` 提交

## 标签命名

### 稳定版发布（核心）

**`v-stable-YYYYMMDD` = 稳定版发布 tag。** 每个 tag 标记一个可部署的稳定基线；`post-checkout` hook 靠它同步 `.nanobot` 配置。

| tag | 日期 | 在当前 lineage | 说明 |
|-----|------|----------------|------|
| `v-stable-20260511` | 05-11 | ✅ | 压缩修复前检查点 |
| `v-stable-20260518` | 05-18 | ✅ | telegram 回调清理 |
| `v-stable-20260523` | 05-23 | ✅ | rank 隔离 |
| `v-stable-20260517` | 05-17 | ❌ | 旧 dev 合并线，历史稳定版，保留 |
| `v-stable-20260605` | 06-05 | ❌ | 另一路线的稳定版，保留 |
| `v-stable-20260527` | 06-16 | ✅ | **当前稳定版**；`456ae97c` |

旧格式 `stable-YYYY-MM-DD`（如 `stable-2026-05-17`）含义相同，不再新建。

**发布流程：** 在 `stable-YYYYMMDD` 分支验证通过后打 `v-stable-YYYYMMDD`（日期与分支一致）。

### 其他 tag 类型（不是稳定版）

| 类型 | 格式 | 含义 |
|------|------|------|
| PyPI 版本 | `v0.x.x` | 包发布 semver，与 `v-stable-*` 独立 |
| 功能里程碑 | `<feature>-YYYYMMDD` | 开发中间检查点，如 `broadcast-ux-polish-20260616` |
| 回滚备份 | `backup-*` / `v-backup-*` | 紧急回滚锚点，非正式发布 |

**勿混淆：** `broadcast-*-20260616` 等功能 tag 在 stable lineage 上，但**不代表稳定版发布**；只有 `v-stable-*` 才是。

**已废弃的格式（勿再创建）：**
- `v0.x.x-stable`（与 semver 重复）
- `stable/...`（带斜杠）
- `015` 等误打别名

## 切换版本

```bash
# 源码 + 匹配 config 子模块
./tools/switch-with-config.sh <branch-or-tag>
```

## Tag 清理

审计与渐进清理脚本（**默认 dry-run，不改动任何 tag**）：

```bash
./tools/tag-cleanup.sh                              # 完整审计
./tools/tag-cleanup.sh --duplicates-only            # 仅重复 tag
./tools/tag-cleanup.sh --execute                    # 删除重复别名（015 等）
./tools/tag-cleanup.sh --tag-stable --date 20260527 --execute  # 发布稳定版 v-stable-20260527
```

当前 46 个 tag 均在本地，远程 0 个。并行开发期间建议只跑 dry-run。

**已发布：** `v-stable-20260527` → `456ae97c`（2026-06-16）。配置仓库需用 `capture-config.sh` 打同名 tag 后 `checkout` 才能完整同步。

## 远程仓库

| Remote | 地址 |
|--------|------|
| `nanobots` | `github.com/l67077955-cyber/nanobots.git` |
| `upstream` | `github.com/HKUDS/nanobot.git`（只读） |

定期清理过期 upstream 跟踪：

```bash
git remote prune upstream
```

## 分支审查记录

### `progressive-fixes`（2026-06-16 审查，已废弃）

15 个 commit 全部已在 `stable-20260527` 中以不同 SHA 存在（经 `main` 合入）：

| progressive-fixes | stable 中的等价 commit | 状态 |
|-------------------|----------------------|------|
| `9b439190` end_discussion 内容丢失 | `ae2c1a26` | 已覆盖 |
| `6b4a0483` busy-replier 死锁 | `19e91046` | 已覆盖 |
| `d728ec64` slot 泄漏 + busy_agents | `6b641659` + `_busy_agents` 检查 | 已覆盖 |
| `5542061f` loopback URL 放行 | `fc4f5045` | 已覆盖 |
| `2efaa8c4` boolean 解析 | `75d0179f` | 已覆盖 |
| `1e901fcd` leader_end_event 检查 | `38901405` | 已覆盖 |
| `5fd27c51` synthesis after end_discussion | `778c00ca` | 已覆盖 |
| `c6f344de` max_chars getter 恢复 | `6f6e8357` | 已覆盖 |
| `ff413832` head_indices 重算 | `c7347921` | 已覆盖 |
| `b8e4e569` safety guard 误报 | `bede0b06` | 已覆盖 |
| `48c968e5` _request_log 上限 | `4ac4fda7` | 已覆盖 |
| `e66aa9a9` end_discussion guard | `8e58eb18` | 已覆盖 |
| `f7b4ea1a` 连续 LLM 错误终止 | `f5f44dd2` | 已覆盖 |
| `680e54da` 退化重复循环检测 | `46cf0831` | 已覆盖 |
| `30330728` v-stable-20260517 合并点 | — | 历史节点 |

**结论：** 不可合并。`progressive-fixes` 比 `stable` 落后 73 个文件（会回退 rank 现代化、ForgetTool、条件 memory_palace 等）。已删除远程分支。

### `dev` ↔ `stable` 分叉合并（2026-06-16）

**策略：** 以 `stable` 为基（保留 token-aware 压缩、forget 警告、rank 现代化等），只移植 `dev` 独有价值。

| dev commit | 处理 | 说明 |
|------------|------|------|
| `6daa75cd` ForgetTool ↔ 压缩协调 | ✅ 移植 | `forgotten_tool_call_ids` + `ignored_tool_call_ids` |
| `48641fe7` ForgetTool 实现 | ✅ 合并 | 实例级 `_ctx`（避免群聊跨 agent 污染）+ stable phase 3b 彻底清除 |
| `7b2326ed` pre-tool_loop prune | ✅ 移植 | broadcast 所有 cycle 路径防消息膨胀 |
| `46331ca2` memory_palace 显示预览 | ✅ 移植 | Telegram 工具活动/结果展示 |
| `68e702df` auto_recall 关键词显示 | ⏭ 跳过 | stable 已移除 autorecall（`62a73281`） |
| `7e9a50a8` auto_store 摘要显示 | ⏭ 跳过 | 同上 |
| `54a5ca16` / `b4c439dd` 清理 dead code | ⏭ 跳过 | stable 仍保留 `visible` 参数 |
| `44440fbe` backup commit | ⏭ 跳过 | 应用 tag 代替 |

合并后：`dev` 已 reset 到 `stable-20260527`（`c70119b6`），两分支同 HEAD。

## 待办（渐进整理）

- [ ] 将 GitHub 默认分支从 `main` 改为 `stable-20260527`（需仓库 Settings 手动操作）
- [x] 合并 `dev` 与 `stable` 的分叉（2026-06-16：以 stable 为基，移植 dev 独有价值）
- [x] 处理 `progressive-fixes`（审查完毕，已废弃删除，2026-06-16）
- [x] 删除僵尸分支 `feat/groupchat-optimization`（2026-06-16 完成）
- [x] `stable-20260527` 设置 upstream 跟踪（2026-06-16 完成）
- [x] prune 过期 `upstream/*` 远程跟踪（19 条，2026-06-16 完成）
- [x] 推送本地 21 个积压 commit 到远程（2026-06-16 完成）
- [x] 发布 `v-stable-20260527` 稳定版 tag（2026-06-16）
- [x] 清理重复 tag：`015`、`v0.1.5-stable`、`v0.1.6-stable`、`stable/pre-history-...`（2026-06-16）
- [ ] 配置仓库打同名 `v-stable-20260527` tag（`capture-config.sh`）