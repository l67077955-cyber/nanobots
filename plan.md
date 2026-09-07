# nanobot-src 架构重构计划

> 创建日期: 2026-09-07
> 状态: 待执行

## 问题概述

经过深度架构分析，识别出以下核心问题：

| 排名 | 问题 | 严重性 |
|------|------|--------|
| 1 | MessageBus入站队列是死代码，只有Telegram正确接线 | 🔴 CRITICAL |
| 2 | Provider层代码重复（litellm/httpx大量重复） | 🔴 CRITICAL |
| 3 | 设置持久化分散到4+处，无统一服务层 | 🔴 CRITICAL |
| 4 | callbacks.py God-object (3299行) | 🟠 HIGH |
| 5 | GroupChatEngine God-object (1680行) | 🟠 HIGH |
| 6 | Channel实现重复（去重/媒体下载/消息分片） | 🟠 HIGH |
| 7 | Skills/Mods两个插件系统边界模糊 | 🟡 MEDIUM |
| 8 | 进程级全局单例 | 🟡 MEDIUM |
| 9 | gateway()组装函数无依赖注入 | 🟡 MEDIUM |
| 10 | Display层Telegram特定 | 🟡 MEDIUM |

---

## Phase 1: 修复核心缺陷 (CRITICAL)

### 1.1 MessageBus入站路由统一

**问题**：`MessageBus.consume_inbound()` 从未在生产代码调用，只有Telegram通过`engine.inject()`绕过总线工作。

**目标**：所有channel通过统一路径接入群聊引擎。

**实施步骤**：

1. 创建 `nanobot/bus/router.py`
   ```python
   class IngressRouter:
       """消费MessageBus入站队列，路由到正确的处理器"""
       
       def __init__(self, engine: GroupChatEngine, bus: MessageBus):
           self._engine = engine
           self._bus = bus
           self._command_handlers: dict[str, Callable] = {}
       
       async def start(self) -> None:
           """启动消费循环"""
           while True:
               msg = await self._bus.consume_inbound()
               await self._route(msg)
       
       async def _route(self, msg: InboundMessage) -> None:
           content = msg.content.strip()
           # 命令路由
           if content.startswith('/'):
               handler = self._command_handlers.get(msg.channel, self._default_command_handler)
               await handler(msg)
           # 群聊消息
           else:
               await self._engine.deliver_user_message(
                   session_key=msg.session_key,
                   content=content,
                   media=msg.media,
                   metadata=msg.metadata,
               )
   ```

2. 修改 `GroupChatEngine` 添加公共接口
   ```python
   async def deliver_user_message(
       self,
       session_key: str,
       content: str,
       media: list[str] | None = None,
       metadata: dict | None = None,
   ) -> None:
       """统一的用户消息入口点，替代inject()"""
       # 迁移 UserIngress 逻辑到这里
   ```

3. 删除 Telegram 的 `inject()` 快捷方式
   - 修改 `channels/telegram/message_handler.py`
   - 删除 `self._groupchat_engine.inject()` 调用
   - 改用 `self._handle_message()` → `bus.publish_inbound()`

4. 在 `gateway()` 中启动路由器
   ```python
   router = IngressRouter(engine, bus)
   asyncio.create_task(router.start())
   ```

5. 验证所有channel工作
   - 为Discord/Feishu/Matrix等添加集成测试
   - 确认群聊消息正确路由

**文件变更**：
- 新建: `nanobot/bus/router.py`
- 修改: `nanobot/bus/__init__.py`
- 修改: `nanobot/groupchat/orchestra/engine.py`
- 修改: `nanobot/channels/telegram/message_handler.py`
- 修改: `nanobot/cli/commands.py`
- 新建: `tests/test_ingress_router.py`

---

### 1.2 Provider共享逻辑提取

**问题**：`litellm_provider.py` 和 `httpx_provider.py` 重复8+个函数。

**目标**：共享逻辑集中在基类或mixin。

**实施步骤**：

1. 在 `providers/base.py` 添加共享helpers
   ```python
   class LLMProvider(ABC):
       # 添加模块级常量（从litellm/httpx提取）
       _ALLOWED_MSG_KEYS = frozenset({...})
       _ANTHROPIC_EXTRA_KEYS = frozenset({...})
       
       @staticmethod
       def _short_tool_id(call_id: str) -> str:
           """规范化tool_call ID"""
           # 从 litellm_provider.py:89 提取
       
       @staticmethod
       def _normalize_tool_call_id(tool_calls: list) -> list:
           """规范化tool_call ID格式"""
           # 从 litellm_provider.py:177 提取
       
       @staticmethod
       def _apply_model_overrides(messages: list, model: str) -> list:
           """应用模型特定的消息覆盖"""
           # 从 httpx_provider.py 提取
   ```

2. 创建 `providers/message_utils.py`（可选，如果base.py过大）
   ```python
   def sanitize_messages(messages: list, allowed_keys: frozenset) -> list:
       """清理消息格式"""
   
   def flatten_tool_messages(messages: list) -> list:
       """将tool协议消息转为纯文本（兼容模式）"""
   
   def parse_tool_calls(response_data: dict) -> list[ToolCallRequest]:
       """解析tool_calls响应"""
   ```

3. 重构 `litellm_provider.py`
   - 删除重复的helpers
   - 导入并使用基类/工具函数
   - 目标：从1167行降至~800行

4. 重构 `httpx_provider.py`
   - 删除重复的helpers
   - 导入并使用基类/工具函数
   - 目标：从843行降至~600行

**文件变更**：
- 修改: `nanobot/providers/base.py`
- 修改: `nanobot/providers/litellm_provider.py`
- 修改: `nanobot/providers/httpx_provider.py`
- 新建（可选）: `nanobot/providers/message_utils.py`

---

### 1.3 统一设置持久化服务

**问题**：`providers_models.json` 有4处读写，`agents/<name>/config.json` 有25+处写点。

**目标**：单一服务层管理所有持久化。

**实施步骤**：

1. 扩展 `state/settings_store.py`
   ```python
   class SettingsStore:
       """统一的配置读写服务"""
       
       def __init__(self, data_dir: Path | None = None):
           self._data_dir = data_dir or Path.home() / ".nanobot"
       
       # Provider/Model 管理
       def load_providers_models(self) -> dict:
           """加载 providers_models.json"""
       
       def save_providers_models(self, data: dict) -> None:
           """保存 providers_models.json"""
       
       def get_provider(self, name: str) -> dict | None:
           """获取单个provider配置"""
       
       def set_provider(self, name: str, config: dict) -> None:
           """设置/更新provider"""
       
       # Agent 配置管理
       def load_agent(self, name: str) -> dict:
           """加载 agent 配置"""
       
       def save_agent(self, name: str, config: dict) -> None:
           """保存 agent 配置"""
       
       def list_agents(self) -> list[str]:
           """列出所有agent"""
       
       # 全局设置
       def load_global_settings(self) -> dict:
           """加载全局设置"""
       
       def save_global_settings(self, settings: dict) -> None:
           """保存全局设置"""
   ```

2. 创建单例获取函数
   ```python
   _store: SettingsStore | None = None
   
   def get_settings_store() -> SettingsStore:
       global _store
       if _store is None:
           _store = SettingsStore()
       return _store
   ```

3. 迁移调用点（分批进行）
   - **第一批**：`providers/litellm_provider.py`, `providers/httpx_provider.py`
   - **第二批**：`channels/telegram/commands/providers.py`
   - **第三批**：`channels/telegram/callbacks.py` 中的 `_save_pm` 调用
   - **第四批**：`skills/settings/scripts/settings_cli.py`

4. 添加测试
   ```python
   # tests/test_settings_store.py
   def test_provider_crud(store: SettingsStore):
       store.set_provider("test", {"api_key": "x"})
       assert store.get_provider("test")["api_key"] == "x"
       store.delete_provider("test")
       assert store.get_provider("test") is None
   ```

**文件变更**：
- 修改: `nanobot/state/settings_store.py`
- 修改: `nanobot/providers/litellm_provider.py`
- 修改: `nanobot/providers/httpx_provider.py`
- 修改: `nanobot/channels/telegram/commands/providers.py`
- 修改: `nanobot/channels/telegram/callbacks.py`
- 修改: `nanobot/skills/settings/scripts/settings_cli.py`
- 新建: `tests/test_settings_store.py`

---

## Phase 2: 降低复杂度 (HIGH)

### 2.1 拆分 callbacks.py

**问题**：3299行单体，包含所有inline-keyboard事件处理。

**目标**：按领域分解，每个<500行。

**实施步骤**：

1. 识别领域边界
   - `AgentCallbacks`: agent相关 (em_* callbacks)
   - `ProviderCallbacks`: provider/model相关 (pm_* callbacks)
   - `SettingsCallbacks`: 设置相关
   - `GroupCallbacks`: 群组相关
   - `HistoryCallbacks`: 历史相关

2. 创建 `channels/telegram/callbacks/` 目录
   ```
   callbacks/
   ├── __init__.py          # CallbacksMixin 组合所有子mixin
   ├── agents.py            # AgentCallbacks
   ├── providers.py         # ProviderCallbacks  
   ├── settings.py          # SettingsCallbacks
   ├── groups.py            # GroupCallbacks
   └── history.py           # HistoryCallbacks
   ```

3. 重构 `CallbacksMixin`
   ```python
   class CallbacksMixin(
       AgentCallbacks,
       ProviderCallbacks,
       SettingsCallbacks,
       GroupCallbacks,
       HistoryCallbacks,
   ):
       """组合所有callback handlers"""
       
       async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
           """路由callback到具体handler"""
           data = update.callback_query.data
           if data.startswith("em_"):
               await self._handle_agent_callback(update, context)
           elif data.startswith("pm_"):
               await self._handle_provider_callback(update, context)
           # ...
   ```

4. 使用 `SettingsStore` 替代直接的文件操作

**文件变更**：
- 新建: `nanobot/channels/telegram/callbacks/__init__.py`
- 新建: `nanobot/channels/telegram/callbacks/agents.py`
- 新建: `nanobot/channels/telegram/callbacks/providers.py`
- 新建: `nanobot/channels/telegram/callbacks/settings.py`
- 删除: `nanobot/channels/telegram/callbacks.py` (旧文件)

---

### 2.2 继续 GroupChatEngine 的绞杀者模式

**问题**：1680行，broadcast/tools直接访问75次私有属性。

**目标**：继续提取，减少engine职责。

**实施步骤**：

1. 提取 `AgentRegistry`
   ```python
   class AgentRegistry:
       """管理可用agents配置"""
       
       def __init__(self, agents_dir: Path):
           self._agents: dict[str, AgentConfig] = {}
       
       def load(self) -> None:
           """加载所有agent配置"""
       
       def get(self, name: str) -> AgentConfig | None:
           """获取agent配置"""
       
       def list(self) -> list[str]:
           """列出所有agent名"""
       
       def add(self, name: str, config: AgentConfig) -> None:
           """添加agent"""
       
       def remove(self, name: str) -> None:
           """移除agent"""
   ```

2. 提取 `ToolRegistryManager`
   ```python
   class ToolRegistryManager:
       """管理per-agent tool registry"""
       
       def __init__(self, workspace: Path, provider: LLMProvider):
           self._cache: dict[str, ToolRegistry] = {}
       
       def get_registry(self, agent_name: str, workspace_scope: str) -> ToolRegistry:
           """获取或创建agent的tool registry"""
       
       def clear_cache(self, agent_name: str) -> None:
           """清除agent的registry缓存"""
   ```

3. 完成 `RoundLifecycle` 迁移
   - 删除 `engine._running` 的所有直接读写
   - 全部通过 `RoundLifecycle` 查询
   - 删除 `leader_end_event` 参数

4. 接口化 `BroadcastContext`
   ```python
   @runtime_checkable
   class BroadcastContext(Protocol):
       """Broadcast所需的引擎接口"""
       @property
       def round(self) -> int: ...
       @property  
       def leader(self) -> str | None: ...
       async def send(self, text: str) -> None: ...
       async def add_message(self, sender: str, content: str) -> None: ...
       # 只暴露必要的方法，不暴露私有属性
   ```

**文件变更**：
- 新建: `nanobot/groupchat/orchestra/agent_registry.py`
- 新建: `nanobot/groupchat/orchestra/tool_registry_manager.py`
- 修改: `nanobot/groupchat/orchestra/engine.py`
- 修改: `nanobot/groupchat/orchestra/broadcast.py`
- 修改: `nanobot/groupchat/orchestra/round_lifecycle.py`

---

### 2.3 提取Channel共享组件

**问题**：去重/媒体下载/消息分片在6+处重复实现。

**目标**：提取可复用的工具类。

**实施步骤**：

1. 创建 `channels/utils/dedup.py`
   ```python
   from collections import OrderedDict
   from typing import Deque
   from collections import deque
   
   class MessageDeduper:
       """消息去重器，支持多种策略"""
       
       def __init__(self, capacity: int = 1000, strategy: str = "ordered"):
           self._capacity = capacity
           self._strategy = strategy
           self._seen: OrderedDict[str, None] | set[str]
           if strategy == "ordered":
               self._seen = OrderedDict()
           else:
               self._seen = set()
       
       def is_duplicate(self, msg_id: str) -> bool:
           """检查并记录消息ID"""
           if msg_id in self._seen:
               return True
           if self._strategy == "ordered":
               self._seen[msg_id] = None
               if len(self._seen) > self._capacity:
                   self._seen.popitem(last=False)
           else:
               self._seen.add(msg_id)
               if len(self._seen) > self._capacity:
                   # 随机淘汰一半
                   ...
           return False
   ```

2. 创建 `channels/utils/media.py`
   ```python
   class MediaDownloader:
       """统一的媒体下载工具"""
       
       def __init__(self, channel_name: str, media_dir: Path | None = None):
           self._media_dir = media_dir or get_media_dir(channel_name)
       
       async def download(self, url: str, filename: str | None = None) -> Path:
           """下载媒体文件到本地"""
       
       async def download_from_attachment(
           self, 
           attachment: dict, 
           http_client: httpx.AsyncClient
       ) -> Path | None:
           """从attachment dict下载"""
   ```

3. 创建 `channels/utils/message.py`
   ```python
   class MessageSplitter:
       """平台感知的消息分割器"""
       
       PLATFORM_LIMITS = {
           "telegram": 4096,
           "discord": 2000,
           "slack": 4000,
           "matrix": 16384,
           "feishu": 30000,
       }
       
       def __init__(self, platform: str, limit: int | None = None):
           self._limit = limit or self.PLATFORM_LIMITS.get(platform, 4096)
       
       def split(self, text: str) -> list[str]:
           """智能分割长消息，保持markdown完整性"""
   ```

4. 迁移各channel使用共享组件
   - Feishu: 使用 `MessageDeduper` 替代 OrderedDict
   - Discord: 使用 `MediaDownloader`
   - 所有channel: 使用 `MessageSplitter`

**文件变更**：
- 新建: `nanobot/channels/utils/__init__.py`
- 新建: `nanobot/channels/utils/dedup.py`
- 新建: `nanobot/channels/utils/media.py`
- 新建: `nanobot/channels/utils/message.py`
- 修改: `nanobot/channels/feishu.py`
- 修改: `nanobot/channels/discord.py`
- 修改: `nanobot/channels/matrix.py`
- 修改: `nanobot/channels/dingtalk.py`
- 修改: `nanobot/channels/wecom.py`
- 修改: `nanobot/channels/qq.py`
- 修改: `nanobot/channels/email.py`
- 修改: `nanobot/channels/whatsapp.py`
- 修改: `nanobot/channels/mochat.py`

---

## Phase 3: 改进架构一致性 (MEDIUM)

### 3.1 明确Skills和Mods边界

**目标**：清晰的职责划分。

**规则**：
- **Skills** (`skills/*.md`): 仅作为prompt文档，教导agent如何使用工具
- **Mods** (`mods/*/mod.py`): Python代码，订阅事件，扩展行为
- **Skills with scripts**: 必须迁移到Mods，或者仅作为CLI工具（不注入prompt）

**实施步骤**：

1. 审计现有skills with scripts
   - `skills/cron/scripts/cron_cli.py` → 保留（CLI工具）
   - `skills/settings/scripts/settings_cli.py` → 迁移核心逻辑到 `SettingsStore`
   - `skills/debug/scripts/send_cli.py` → 保留（CLI工具）

2. 更新文档
   - 在 `docs/SKILL_VS_MOD.md` 明确边界
   - 更新 `AGENTS.md`

3. 强制执行（可选）
   - `SkillsLoader` 警告包含scripts的skill
   - 提供迁移指南

---

### 3.2 引入上下文对象替代全局单例

**目标**：支持多会话、易测试。

**实施步骤**：

1. 创建 `AppContext`
   ```python
   @dataclass
   class AppContext:
       """应用级上下文，替代全局单例"""
       event_bus: BroadcastEventDispatcher
       settings_store: SettingsStore
       mod_manager: ModManager
       data_dir: Path
       
       @classmethod
       def create(cls, data_dir: Path | None = None) -> "AppContext":
           """创建应用上下文"""
           data_dir = data_dir or Path.home() / ".nanobot"
           event_bus = BroadcastEventDispatcher()
           settings_store = SettingsStore(data_dir)
           mod_manager = ModManager(settings_store)
           return cls(
               event_bus=event_bus,
               settings_store=settings_store,
               mod_manager=mod_manager,
               data_dir=data_dir,
           )
   ```

2. 修改 `gateway()` 使用上下文
   ```python
   def gateway():
       ctx = AppContext.create()
       engine = GroupChatEngine(..., context=ctx)
       router = IngressRouter(engine, ctx)
       # ...
   ```

3. 逐步替换 `get_bus()` 等全局函数
   - 优先级低，可在Phase 4进行

---

### 3.3 引入依赖注入容器（可选）

**目标**：`gateway()` 可测试。

**实施步骤**：

1. 使用 `dependency-injector` 或简单DI
   ```python
   from dependency_injector import containers, providers
   
   class Container(containers.DeclarativeContainer):
       config = providers.Singleton(load_config)
       bus = providers.Singleton(MessageBus)
       settings_store = providers.Singleton(SettingsStore)
       provider = providers.Singleton(LiteLLMProvider, config=config.provided)
       engine = providers.Singleton(GroupChatEngine, ...)
   ```

2. 重构 `gateway()` 使用容器
   ```python
   def gateway():
       container = Container()
       engine = container.engine()
       router = container.router()
       # ...
   ```

---

### 3.4 抽象Display接口

**目标**：非Telegram channel也有状态面板能力。

**实施步骤**：

1. 定义 `StatusPanel` 协议
   ```python
   @runtime_checkable
   class StatusPanel(Protocol):
       async def create(self, agents: list[str]) -> None: ...
       async def update(self, agent: str, state: str, detail: str = "") -> None: ...
       async def close(self) -> None: ...
   ```

2. 实现 `TelegramStatusPanel`
   ```python
   class TelegramStatusPanel:
       def __init__(self, edit_fn: Callable, send_fn: Callable):
           self._edit_fn = edit_fn
           self._send_fn = send_fn
           self._msg_id: int | None = None
       
       async def create(self, agents: list[str]) -> None:
           self._msg_id = await self._send_fn(self._render())
       
       async def update(self, agent: str, state: str, detail: str = "") -> None:
           # throttled edit
           await self._edit_fn(self._msg_id, self._render())
   ```

3. 其他channel实现（可占位）
   - `DiscordStatusPanel`: 使用embed
   - `MatrixStatusPanel`: 使用room state
   - `NullStatusPanel`: 无操作

---

## Phase 4: 清理遗留代码 (LOW)

### 4.1 解决循环依赖

- 审计 `lazy import` 注释
- 重新组织 `tools/` 和 `groupchat/` 的导入关系
- 可能需要引入中间层

### 4.2 统一Session和HistoryContext

- 决定保留哪个
- 迁移或删除另一个
- 更新所有调用点

### 4.3 删除 `engine._running` 遗留flag

- 确认所有调用点已迁移到 `RoundLifecycle`
- 删除属性
- 删除 `mark_winding_down(flip_running=True)` 参数

---

## 执行顺序建议

```
Phase 1 (必须先完成)
├── 1.1 MessageBus路由 (最高优先级，修复核心缺陷)
├── 1.2 Provider共享逻辑
└── 1.3 Settings持久化服务

Phase 2 (Phase 1完成后)
├── 2.1 拆分callbacks.py (依赖1.3)
├── 2.2 Engine绞杀者模式 (可并行)
└── 2.3 Channel共享组件 (可并行)

Phase 3 (Phase 2完成后)
├── 3.1 Skills/Mods边界 (可并行)
├── 3.2 上下文对象 (依赖1.3)
├── 3.3 依赖注入 (依赖3.2)
└── 3.4 Display接口 (可并行)

Phase 4 (清理)
└── 所有遗留问题
```

---

## 测试策略

每个Phase需要：

1. **单元测试**
   - 新建的类/函数必须有测试
   - 覆盖率目标: 80%+

2. **集成测试**
   - Phase 1.1 完成后：所有channel的群聊端到端测试
   - Phase 1.3 完成后：设置持久化一致性测试

3. **回归测试**
   - 每次修改后运行 `pytest tests/ -q`
   - 关键路径：Telegram群聊流程、设置保存/加载

4. **手动验证**
   - Phase 1.1 完成后：重启gateway，测试多channel
   - Phase 2.1 完成后：测试Telegram管理界面

---

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| MessageBus路由引入新bug | 高 | 高 | 先写集成测试，小步验证 |
| Settings迁移遗漏调用点 | 中 | 中 | 搜索所有 `_save_pm` / `config.json` 写点 |
| callbacks拆分破坏现有流程 | 中 | 高 | 保持接口不变，内部重构 |
| Engine重构影响性能 | 低 | 低 | 性能基准测试 |

---

## 参考文档

- `docs/design-review-2026-08-07.md` - 项目自己的设计审查
- `AGENTS.md` - 项目级约定
- `nanobot/groupchat/orchestra/round_lifecycle.py` 注释 - 状态机迁移背景

---

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-09-07 | 初始版本，基于架构深度分析 |
| 2026-09-07 | Phase 1 完成：MessageBus路由(1.1)、Provider共享逻辑(1.2)、Settings持久化服务(1.3) |
| 2026-09-07 | Phase 2 部分完成：callbacks骨架(2.1)、Channel共享组件(2.3) |
