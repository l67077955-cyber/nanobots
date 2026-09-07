# Nanobot 版本控制规范

> v2 | 生效日期：2026-09-07 | 取代 v1 (2026-09-01)

---

## 0. v1 发生了什么

v1 认定 `fix/groupchat-headless-stable-align` 为事实主线，要求所有新分支以它为基线。

**这个计划从未被执行。** v1 写完之后，该分支再没有收到过一个 commit，而 `feat/ui-redesign`
继续积累了 67 个提交（history 重构、mods 插件架构、skills 兼容、429 限流等）。到 09-07 时，
两条线在 `callbacks` 和 `orchestra`/`runtime` 两处都发生了目录级分歧，试合并冲突 47 个文件。

更严重的是分叉已经开始造成重复劳动：History 子系统在两条分支上被**各自独立重写了一遍**
（align 的 `f0fed5f9`，ui-redesign 的 Phase A–D）。

v2 因此把结论反过来：**承认 `feat/ui-redesign` 为主线并扶正为 `main`**。

理由是重做代价的不对称 —— align 的优势（`callbacks/` 拆包、`orchestra/`→`runtime/` 改名）
都是机械重构，可以在新主线上重做；ui-redesign 的 67 个提交是行为变更，无法重做。

---

## 1. 分支模型

```
main                    唯一主线，所有工作的基线
  ├─ feat/*             新功能
  ├─ fix/*              缺陷修复
  ├─ refactor/*         重构
  └─ experimental/*     架构级实验（预期可能被丢弃）
```

**规则**

- 一切以 `main` 为基线开分支，合并回 `main` 后删除。
- 分支存活超过 **两周**必须 rebase 或合并回主线。v1 的教训是分叉三个半月后已无法收敛。
- 不再设"事实主线"这种概念。`main` 就是主线；如果实际工作不在 `main` 上，说明该扶正了，
  而不是该写文档承认现状。

### 归档

| Tag | 指向 | 说明 |
|---|---|---|
| `archive/main-20260706` | `d2bf4cf9` | 扶正前的旧 `main`，停滞于 07-06 |
| `archive/align-20260720` | `24e3f0b4` | v1 认定的"事实主线"，停滞于 07-20 |

### 待办：从 `archive/align-20260720` 前向移植

这两项只存在于归档分支，需要在 `main` 上作为独立提交重做：

1. `nanobot/channels/telegram/callbacks.py`（单体，170KB）→ `callbacks/` 包（11 模块）
2. `nanobot/groupchat/orchestra/` → `nanobot/groupchat/runtime/` 目录改名
3. 该分支多出的约 20 个测试

---

## 2. Tag 规范

只保留四族，其余一律归入 `archive/`：

| 模式 | 用途 | 可变性 |
|---|---|---|
| `v<major>.<minor>.<patch>` | 正式发布 | 不可移动 |
| `stable-<YYYYMMDD>-<desc>` | 里程碑稳定版本 | 不可移动 |
| `running-<YYYYMMDD>-<HHMM>` | 线上部署点 | 不可移动，累积保留 |
| `archive/<desc>-<YYYYMMDD>` | 废弃分支/回滚前快照 | 不可移动 |

历史上遗留的 `backup-before-rollback-*`、`v-stable-*`、`v-backup-*`、
`ui-redesign-before-*`、`prompt-*`、`broadcast-*` 等命名族不再新增。

`pyproject.toml` 的 `version` 必须与最近的 `v*` tag 一致。当前是脱节的
（`0.1.4.post5` vs tag `v0.2.2`），发下一个版本时一并对齐。

---

## 3. 部署与回滚

### 3.1 开发与部署分离

**开发工作区和线上运行的代码不共用一个 checkout。**

```
/root/projects/nanobot-src   开发工作区，HEAD 跟着你走
/root/nanobot-deploy         部署 worktree，detached，只指向 running-* tag
/root/nanobot-src            symlink → 部署 worktree
```

v1 的回滚流程要求在开发工作区里 `git checkout <tag>`，那会把你的开发分支 detach 掉，
而且线上跑的和你正在编辑的是同一份文件。现在回滚只动部署 worktree：

```bash
git -C /root/nanobot-deploy checkout --detach <running-tag>
sudo systemctl restart nanobot-gateway.service
```

### 3.2 关键认知

**Python 已把旧代码加载进内存，改磁盘文件不影响当前进程。** 必须重启
`nanobot-gateway.service` 才生效。反过来说，磁盘上的 HEAD 不等于线上正在跑的版本 ——
以最后一次重启时的 `running-*` tag 为准。

### 3.3 重启前检查

```bash
git -C /root/nanobot-deploy status --short     # 工作区必须干净
git -C /root/nanobot-deploy log --oneline -1   # 确认是预期 commit
python3 -m pytest tests/ -q                     # 全量测试
sudo systemctl restart nanobot-gateway.service
sudo journalctl -u nanobot-gateway.service -n 50 --no-pager
```

---

## 4. CI

`.github/workflows/ci.yml` 在**所有分支**上触发（`branches: ['**']`），Python 3.11/3.12/3.13。

v1 时期 CI 只在 `main`/`nightly` 上跑，而所有工作都在 `feat/ui-redesign` —— CI 最后一次
测到的代码停留在 7 月。触发范围必须覆盖实际开发的分支，否则等于没有。

---

## 5. 配置仓库 (`/root/.nanobot`)

`pre-push` hook 会在配置仓库有未提交变更时拒绝推送代码。要让这个 hook 有意义，
配置仓库里就**不能有自己会变的文件**。

判断标准很简单：

> **这个字段回滚之后，你希望它变回旧值吗？**
> 是 → 版本化；否 → gitignore。

运行时状态属于后者 —— 它必须反映现在，恢复旧值是有害的（例如 cron 的
`lastRunAtMs` 回退会导致补跑，`nextRunAtMs` 会指向过去的时间点）。

已按此拆分：

| 文件 | 内容 | 版本化 |
|---|---|---|
| `cron/jobs.json` | 任务定义：`schedule`、`payload`、`enabled` | ✅ |
| `cron/state.json` | 运行时状态：`nextRunAtMs`、`runHistory` 等 | ❌ gitignore |

新增配置文件时按同样标准判断，不要把两类数据混在一个文件里。
