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

## 待办（渐进整理）

- [ ] 将 GitHub 默认分支从 `main` 改为 `stable-20260527`
- [ ] 合并 `dev` 与 `stable` 的分叉（ForgetTool 等待统一）
- [ ] 处理 `progressive-fixes`（合并或废弃）
- [x] 删除僵尸分支 `feat/groupchat-optimization`
- [ ] 清理重复 tag（`v0.1.5` / `v0.1.5-stable` 等）