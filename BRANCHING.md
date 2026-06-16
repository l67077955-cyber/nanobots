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

统一使用以下格式，旧 tag 逐步废弃：

| 类型 | 格式 | 示例 |
|------|------|------|
| 稳定快照 | `v-stable-YYYYMMDD` | `v-stable-20260605` |
| 回滚备份 | `backup-YYYYMMDD-HHMMSS` | `backup-20260608-165600` |
| 功能里程碑 | `<feature>-YYYYMMDD` | `prompt-config-overhaul-20260616` |

**已废弃的格式（勿再创建）：**
- `stable-YYYY-MM-DD`（带横杠的日期）
- `v0.x.x-stable`（与 semver 重复）
- `stable/...`（带斜杠）

## 切换版本

```bash
# 源码 + 匹配 config 子模块
./tools/switch-with-config.sh <branch-or-tag>
```

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
- [ ] 清理重复 tag（`v0.1.5` / `v0.1.5-stable` 等）