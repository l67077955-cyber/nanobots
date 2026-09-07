# Skills vs Mods: 边界与职责

> 创建日期: 2026-09-07
> 状态: 规范文档

## 核心区分

| 特性 | Skills | Mods |
|------|--------|------|
| **本质** | Prompt文档，教导agent如何使用工具 | Python代码，订阅事件并扩展行为 |
| **位置** | `skills/*.md` 或 `skills/*/SKILL.md` | `~/.nanobot/mods/<name>/mod.py` |
| **运行时** | 被agent读取并理解 | 被事件总线调用执行 |
| **副作用** | 无（纯文档） | 有（可发送消息、写文件等） |
| **可测试性** | 通过agent行为验证 | 单元测试 + 集成测试 |

## Skills 规范

### 定义
Skills 是**纯文档**，用于：
1. 教导 agent 如何使用特定工具或工作流
2. 提供领域知识、最佳实践、参考文档
3. 定义 prompt 模板和示例

### 允许的内容
- `SKILL.md` - 主文档（必需）
- `references/*.md` - 参考文档
- `assets/*` - 模板、示例文件

### 禁止的内容
- ❌ Python脚本（`scripts/*.py`）
- ❌ 可执行代码
- ❌ 运行时逻辑

### CLI工具例外
如果 Skill 必须提供脚本，**仅允许**作为CLI工具存在：
- 脚本必须在 `scripts/` 目录
- 脚本必须是独立的CLI，**不注入prompt**
- 脚本文档必须明确标注"CLI工具"而非"agent行为扩展"

**合规示例**：
- `cron/scripts/cron_cli.py` - CLI管理定时任务（不改变agent行为）
- `debug/scripts/send_cli.py` - CLI发送测试消息（调试工具）
- `skill-creator/scripts/init_skill.py` - CLI创建skill目录

**不合规示例**：
- ❌ `settings/scripts/settings_cli.py` - 应迁移逻辑到 `SettingsStore`，CLI仅作为薄封装
- ❌ 任何被 `SkillsLoader` 作为prompt注入的脚本

## Mods 规范

### 定义
Mods 是**行为扩展**，用于：
1. 订阅事件并响应
2. 添加运行时行为（提醒、统计、过滤等）
3. 扩展核心功能而不修改核心代码

### 结构
```python
from nanobot.mods.base import Mod

class MyMod(Mod):
    name = "my_mod"
    description = "简短描述"

    def default_config(self):
        return {"enabled": False}

    async def start(self, ctx):
        # 初始化逻辑
        pass

    async def on_user_message_delivered(self, *, message, **kw):
        # 事件处理逻辑
        pass
```

### 事件订阅
- 方法名：`on_<event_name>`（event `user:message_delivered` → `on_user_message_delivered`）
- 总是接受 `**kw` 以兼容新增字段
- Tier 1（观察型）：读取payload，使用 `ctx.send`
- Tier 2（过滤型）：仅往可变容器append，不替换

详见 `docs/MOD_PLUGIN_GUIDE.md`。

## 迁移指南

### 当 Skill 包含行为脚本时

1. **评估脚本目的**
   - 是纯CLI工具？→ 保留，文档标注
   - 是扩展agent行为？→ 迁移到Mod

2. **迁移到Mod**
   ```bash
   # 1. 创建mod目录
   mkdir -p ~/.nanobot/mods/my_feature

   # 2. 创建mod.py
   # 将脚本逻辑迁移到事件处理方法

   # 3. 删除skill/scripts中的脚本
   # 或保留为CLI工具（如果适用）
   ```

3. **更新文档**
   - 在 Skill 中引用 Mod（如适用）
   - 删除脚本相关说明（如果已迁移）

### 当 Settings 需要持久化时

使用 `nanobot.state.settings_store.SettingsStore`：
```python
from nanobot.state.settings_store import get_settings_store

store = get_settings_store()
store.set_provider("openrouter", {...})
```

不要直接读写 `providers_models.json`。

## 审计现状

### Skills with scripts（已审计）

| Skill | Script | 分类 | 行动 |
|-------|--------|------|------|
| cron | `cron_cli.py` | CLI工具 | ✅ 保留 |
| settings | `settings_cli.py` | CLI + 逻辑 | ✅ 逻辑已迁移到SettingsStore |
| debug | `send_cli.py` | CLI工具 | ✅ 保留 |
| debug | `send_photo_cli.py` | CLI工具 | ✅ 保留 |
| skill-creator | `init_skill.py` | CLI工具 | ✅ 保留 |
| skill-creator | `quick_validate.py` | CLI工具 | ✅ 保留 |
| skill-creator | `package_skill.py` | CLI工具 | ✅ 保留 |

### Mods（builtin）

| Mod | 位置 | 状态 |
|-----|------|------|
| antirepeat | `nanobot/mods/builtin/antirepeat.py` | ✅ 已迁移自inline代码 |
| round_telemetry | `nanobot/mods/builtin/round_telemetry.py` | ✅ 新增 |

## 强制执行

### SkillsLoader 警告（可选）

可在 `nanobot/skills/loader.py` 添加检查：
```python
if (skill_dir / "scripts").exists():
    logger.warning(
        f"Skill '{name}' contains scripts/ directory. "
        "If these are not CLI tools, migrate to Mods. "
        "See docs/SKILL_VS_MOD.md"
    )
```

### 代码审查标准

在 PR 审查中：
- ❌ 拒绝添加行为到核心文件（broadcast/mailbox/engine）
- ✅ 要求新行为通过Mod实现
- ✅ 检查Skills中的scripts是否为纯CLI

## 参考

- `docs/MOD_PLUGIN_GUIDE.md` - Mod开发指南
- `nanobot/groupchat/orchestra/events.py` - 事件目录
- `nanobot/state/settings_store.py` - 统一设置服务
- `AGENTS.md` - 项目级约定
