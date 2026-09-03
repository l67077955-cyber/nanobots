---
name: project-conventions
description: "在 nanobot-src 仓库写代码时的强制规范。改任何 .py 之前必读。触发：编码、修 bug、改 nanobot 源码、加功能、写测试。"
always: true
---

# 项目编码红线（nanobot-src）

完整版：`/root/nanobot-src/AGENTS.md`（编码前 read_file 它）。核心五条：

1. **先写回归测试，再修 bug**——没有钉住行为的测试不许动实现。
2. **加行为 = 写 mod**（`~/.nanobot/mods/<名字>/mod.py`，见 docs/MOD_PLUGIN_GUIDE.md），
   核心文件只修核心 bug；禁止最小 diff 止血式护栏堆积。
3. **确认死代码就删**（零引用/零生产者），commit 里写明证据。
4. **编辑前重读目标代码块**——本仓库多 agent 并行编辑，行号不可信。
5. **改后必验**：`python3 -m py_compile <改动文件>` + `pytest tests/ -q` 全绿才算完成。

架构不变量（改动必须保持）：RoundLifecycle 是轮次状态唯一归属；
UserIngress 是消息队列唯一决策点；BroadcastView 纯渲染无控制流。
