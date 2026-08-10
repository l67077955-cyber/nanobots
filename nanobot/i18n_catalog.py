"""Translation catalog for the Telegram UI pilot.

Pilot scope: the /menu object-first panels (root, agents, providers, groups,
logs) and their create-side prompts. Chinese is source-of-truth.

Importing this module registers its entries into the shared :data:`i18n`
catalog. Designed to be appended to as more surfaces are ported.
"""
from nanobot.i18n import i18n

_MENU: dict[str, dict[str, str]] = {
    # ── root panel ─────────────────────────────────────────
    "ui.menu.root.title": {
        "zh": "🎛️ **管理面板**\n\n选择要管理的内容 — 进入后点击对象即可操作。\n(`/help` 仍可查看全部斜杠命令)",
        "en": "🎛️ **Management Panel**\n\nChoose what to manage — then tap an object to act on it.\n(`/help` still lists all slash commands)",
    },
    "ui.menu.root.back_title": {
        "zh": "🎛️ **管理面板**\n\n选择要管理的内容:",
        "en": "🎛️ **Management Panel**\n\nChoose what to manage:",
    },
    "ui.menu.root.agents": {"zh": "🤖 Agent 管理", "en": "🤖 Agents"},
    "ui.menu.root.providers": {"zh": "🏢 提供商 & 模型", "en": "🏢 Providers & Models"},
    "ui.menu.root.groups": {"zh": "👥 分组 & 编排", "en": "👥 Groups & Orchestration"},
    "ui.menu.root.logs": {"zh": "📊 日志", "en": "📊 Logs"},

    # ── agents panel ────────────────────────────────────────
    "ui.agents.empty": {"zh": "🤖 暂无 agent\n\n用 /newagent 添加", "en": "🤖 No agents yet\n\nUse /newagent to add one"},
    "ui.agents.list_title": {"zh": "🎛️ **Agent 管理** — 选择一个 agent 进行编辑:", "en": "🎛️ **Agent Management** — pick an agent to edit:"},
    "ui.agents.row": {"zh": "{n} — {model}", "en": "{n} — {model}"},
    "ui.agents.new": {"zh": "➕ 新建 Agent", "en": "➕ New Agent"},
    "ui.common.back": {"zh": "⬅️ 返回", "en": "⬅️ Back"},
    "ui.common.cancel": {"zh": "⬅️ 取消", "en": "⬅️ Cancel"},

    # ── providers panel ─────────────────────────────────────
    "ui.providers.empty": {"zh": "🏢 暂无提供商\n\n用 /newprovider 添加", "en": "🏢 No providers yet\n\nUse /newprovider to add one"},
    "ui.providers.list_title": {"zh": "🎛️ **提供商 & 模型** — 选择提供商进行管理:", "en": "🎛️ **Providers & Models** — pick a provider to manage:"},
    "ui.providers.row": {"zh": "🏢 {name} — {url}", "en": "🏢 {name} — {url}"},
    "ui.providers.new": {"zh": "➕ 添加提供商", "en": "➕ Add Provider"},
    "ui.models.new": {"zh": "➕ 添加模型", "en": "➕ Add Model"},

    # ── groups panel ────────────────────────────────────────
    "ui.groups.title": {
        "zh": "👥 **分组管理**\n\n/groups — 查看所有分组\n/savegroup <名称> — 保存当前成员\n/loadgroup <名称> — 载入分组\n/delgroup <名称> — 删除分组\n/order — 调整发言顺序\n/setleader <name> — 设置/取消 Leader 👑",
        "en": "👥 **Group Management**\n\n/groups — list all groups\n/savegroup <name> — save current members\n/loadgroup <name> — load a group\n/delgroup <name> — delete a group\n/order — adjust speaking order\n/setleader <name> — set/unset Leader 👑",
    },

    # ── logs panel ──────────────────────────────────────────
    "ui.logs.title": {
        "zh": "📊 **日志**\n\n/log — 浏览 LLM 调用记录(分页/状态/token/延迟)\n/log <关键词> — 按 agent/模型/内容搜索\n/history — 查看会话历史",
        "en": "📊 **Logs**\n\n/log — browse LLM request logs (paged/status/token/latency)\n/log <keyword> — search by agent/model/content\n/history — view session history",
    },
    "ui.logs.open": {"zh": "📊 打开日志浏览", "en": "📊 Open log browser"},

    # ── create-side prompts ─────────────────────────────────
    "ui.create.agent_prompt": {
        "zh": "🆕 创建新 Agent\n\n请输入 Agent 名字:",
        "en": "🆕 Create Agent\n\nEnter the agent name:",
    },
    "ui.create.provider_prompt": {
        "zh": "🆕 创建提供商\n\n请输入提供商名称 (如 openrouter, aihubmix):",
        "en": "🆕 Create Provider\n\nEnter provider name (e.g. openrouter, aihubmix):",
    },
    "ui.create.need_provider": {
        "zh": "⚠️ 还没有提供商\n\n请先添加提供商:",
        "en": "⚠️ No providers yet\n\nAdd one first:",
    },
    "ui.create.add_provider_btn": {"zh": "➕ 添加提供商", "en": "➕ Add Provider"},
    "ui.create.model_pick_title": {
        "zh": "🆕 添加模型\n\n选择提供商 (再输入模型ID):",
        "en": "🆕 Add Model\n\nPick a provider (then enter model ID):",
    },
}

# Register the block.
i18n.register_many(_MENU)