"""Parameter documentation for /history settings UI."""

PARAM_DOCS: dict[str, dict[str, str]] = {
    "__top__:context_window_tokens": {
            "label": "上下文窗口 (tokens)",
            "location": "全局 → 贯穿整个上下文管理链",
            "doc": (
                "LLM 单次请求可接收的最大 token 数。所有裁剪、"
                "合并策略的上限锚点。\n\n"
                "算法链路:\n"
                "  1. context_pruning 按此值的 30%/50% "
                "触发 soft/hard 裁剪\n"
                "  2. MemoryConsolidator 在达到 50% 时"
                "将旧消息合并为摘要\n"
                "  3. 最终发送给 LLM 的 prompt 不超过此值\n\n"
                "建议: 与你使用的模型窗口匹配 (如 GPT-4.1 → 1,000,000)"
            ),
        },
        "__top__:tool_result_max_chars": {
            "label": "工具结果截断上限 (字符)",
            "location": "全局 → 未知工具 fallback",
            "doc": (
                "仅当工具名不在 exec/web_fetch/web_search 映射中时使用。\n\n"
                "已知工具走 process_tool_result 的 per-tool 上限。\n"
                "tool_loop 默认 result_max_chars 也读此值，但广播/直接模式"
                "会传入 broadcast/direct_result_max_chars (目前未接入注入路径)。\n\n"
                "建议: 应 ≥ 各工具 max_chars 中的最大值"
            ),
        },
        "tool_results:exec_max_chars": {
            "label": "exec 工具截断 (字符)",
            "location": "Stage 1 → 命令执行输出",
            "doc": (
                "shell 命令 (exec tool) 返回结果的最大字符数。\n\n"
                "场景: pip install、git log、ls -la 等命令\n"
                "截断方式: head(前半) + '(N chars truncated)' + tail(后半)\n"
                "下游关系: 截断后若仍超 summarize_threshold → "
                "进入 Stage 2 AI 总结\n\n"
                "重要: 若此值 ≤ summarize_threshold，"
                "则 AI 总结永远不会被触发 (先截断了)"
            ),
        },
        "tool_results:web_fetch_max_chars": {
            "label": "web_fetch 截断 (字符)",
            "location": "Stage 1 → process_tool_result",
            "doc": (
                "web_fetch 工具 (URL 抓取) 返回内容的最大字符数。\n\n"
                "场景: 抓取网页/API 的 HTML→Markdown 转换结果\n"
                "截断方式: head_only — 保留头部 + truncated 标记\n"
                "位置: result_processor.py\n\n"
                "建议: 一般 8,000-15,000 即可覆盖正文"
            ),
        },
        "tool_results:web_search_max_chars": {
            "label": "web_search 截断 (字符)",
            "location": "Stage 1 → process_tool_result",
            "doc": (
                "web_search 工具返回的搜索结果最大字符数。\n\n"
                "场景: 搜索引擎结果摘要列表\n"
                "截断方式: head_only — 保留头部 + truncated 标记\n"
                "位置: result_processor.py\n\n"
                "建议: 5,000-10,000 即可包含足够条目"
            ),
        },
        "tool_results:summarize_threshold": {
            "label": "AI 压缩阈值 (字符)",
            "location": "压缩策略 → process_tool_result",
            "doc": (
                "工具输出超过此字符数时，调用小模型提取关键信息。\n\n"
                "流程: 原始输出 → AI 提取要点 → 截断 → 注入上下文\n"
                "注意: AI 压缩在截断之前执行 (先压缩后截断)\n"
                "失败兜底: 直接 head+tail 截断\n\n"
                "关键约束: 应 >= 各工具截断上限\n"
                "  否则输出在截断后已低于阈值，AI 压缩永远不触发\n\n"
                "建议: 设为各工具截断值的 1.0-1.5 倍"
            ),
        },
        "tool_results:summarize_enabled": {
            "label": "AI 压缩开关",
            "location": "压缩策略 → process_tool_result",
            "doc": (
                "控制工具输出是否经 AI 压缩。\n\n"
                "已接入 process_tool_result: 工具输出超过 summarize_threshold 时"
                "调用 summarize_model 提取要点。\n"
                "历史压缩另由 history.history_summarize_enabled 控制。\n\n"
                "建议: 长对话保持开启以节省上下文"
            ),
        },
        "tool_results:summarize_model": {
            "label": "总结模型",
            "location": "Stage 2 → 总结用 LLM",
            "doc": (
                "用于压缩工具输出的轻量模型。通过 OpenRouter 调用。\n\n"
                "要求: 低延迟、低成本、能准确提取关键信息\n"
                "配置: 也可在 ~/.nanobot/agents/reader/config.json 覆盖"
            ),
        },
        "tool_results:summarize_max_input_chars": {
            "label": "总结器最大输入 (字符)",
            "location": "Stage 2 → 总结器调用",
            "doc": (
                "发送给总结模型的最大输入字符数。\n\n"
                "工具输出先按此长度截断，再发给小模型提取要点。\n"
                "过小会丢失尾部信息，过大会增加 nano 模型成本。\n\n"
                "建议: 与 summarize_threshold 保持一致或略大"
            ),
        },
        "tool_results:summarize_max_output_chars": {
            "label": "总结器最大输出 (tokens)",
            "location": "Stage 2 → 总结器调用",
            "doc": (
                "总结模型生成摘要的最大 token 数 (max_tokens)。\n\n"
                "控制摘要的最大长度。过小可能截断关键信息，"
                "过大则摘要冗长、上下文膨胀。\n\n"
                "建议: 2000-6000"
            ),
        },
        "tool_results:broadcast_result_max_chars": {
            "label": "广播模式 result_max_chars",
            "location": "压缩策略 → broadcast tool_loop (仅 dedup)",
            "doc": (
                "传入 broadcast tool_loop 的 result_max_chars 参数。\n\n"
                "当前仅用于 dedup 缓存截断，不截断注入 messages 的内容。\n"
                "实际截断由 process_tool_result 完成。\n\n"
                "建议: 15,000-30,000"
            ),
        },
        "tool_results:direct_result_max_chars": {
            "label": "直接模式 result_max_chars",
            "location": "压缩策略 → direct tool_loop (仅 dedup)",
            "doc": (
                "传入 direct/serial tool_loop 的 result_max_chars。\n\n"
                "当前仅用于 dedup 缓存截断，不截断注入 messages 的内容。\n\n"
                "建议: 6,000-12,000"
            ),
        },
        "history:max_messages": {
            "label": "最大消息条数",
            "location": "Stage 3 → 历史窗口裁剪",
            "doc": (
                "对话历史中保留的最大消息数量。\n\n"
                "算法: 超过时从最早的消息开始丢弃，"
                "保证 assistant tool_call 与 tool result 配对完整\n"
                "位置: session/manager.py get_history()\n\n"
                "与 max_context_chars 的关系:\n"
                "  两个限制取先触发者 — 哪个先到就执行裁剪\n"
                "  若消息数很少但单条很长 → max_context_chars 先触发\n"
                "  若消息多但都很短 → max_messages 先触发\n\n"
                "建议: 根据平均消息长度调整，"
                "确保与 max_context_chars 匹配"
            ),
        },
        "history:max_context_chars": {
            "label": "最大上下文字符数",
            "location": "Stage 3 → 历史窗口裁剪",
            "doc": (
                "对话历史的总字符数上限。\n\n"
                "算法: sum(所有消息 content 长度)，超过时从最早丢弃\n"
                "位置: groupchat/prompt_builder.py 构建 prompt 时检查\n\n"
                "与 context_window_tokens 的关系:\n"
                "  此值是字符数，context_window 是 token 数\n"
                "  粗略换算: 1 token ≈ 4 字符 (英文) / 2 字符 (中文)\n"
                "  建议此值 ≤ context_window_tokens × 2\n\n"
                "与 max_messages 的关系: 两者取先触发"
            ),
        },
        "history:compress_ratio": {
            "label": "历史压缩触发比例",
            "location": "Stage 3 → 历史压缩",
            "doc": (
                "当消息数达到 max_messages × 此比例时，\n"
                "触发历史压缩（将最早一半消息用小模型摘要）。\n\n"
                "值域: 0.0-1.0，默认 0.8\n"
                "建议: 0.7-0.9"
            ),
        },
        "history:token_trigger_ratio": {
            "label": "token 触发比例",
            "location": "Stage 3 → maybe_compress 触发条件",
            "doc": (
                "当 token 用量 / context_window_tokens ≥ 此比例时，\n"
                "即使消息数尚未达 compress_ratio 也会触发历史压缩\n"
                "(应对单条长消息/工具输出撑爆上下文)。\n\n"
                "此前为硬编码 0.55、不可配且静默凌驾 compress_ratio；\n"
                "现独立可调。与 context_pruning.soft_ratio (tool_loop\n"
                "工具结果裁剪) 是不同机制，可分别调。\n\n"
                "值域: 0.0-1.0，默认 0.55"
            ),
        },
        "history:context_budget_ratio": {
            "label": "历史 token 预算占比",
            "location": "Stage 3 → add_message 裁剪",
            "doc": (
                "add_message 的 in-memory token 预算 =\n"
                "context_window_tokens × 此比例 (为 system prompt/\n"
                "工具等留余量)。超出则按 head 保护丢弃最旧。\n\n"
                "此前为硬编码 0.65；现可调。\n"
                "max_context_chars 仍作为字符安全网在\n"
                "history_to_messages → trim_llm_messages 二次执行。\n\n"
                "值域: 0.0-1.0，默认 0.65"
            ),
        },
        "history:compress_fallback_chars": {
            "label": "回退压缩字数预算",
            "location": "Stage 3 → maybe_compress 机械回退",
            "doc": (
                "AI 摘要不可用或失败时，build_compress_message 将中间段\n"
                "机械压成单个压缩块，此值为其字符预算 (每条留首行预览)。\n\n"
                "与 compress_max_summary_tokens (AI 摘要 token 预算)、\n"
                "tool_log_preview._total_cap (单条工具预览上限) 是三个\n"
                "不同预算，互不影响。此前为硬编码 2000。\n\n"
                "默认 2000"
            ),
        },
        "history:compress_max_summary_tokens": {
            "label": "历史压缩摘要长度 (tokens)",
            "location": "Stage 3 → maybe_compress",
            "doc": (
                "History.compress_middle 调用 summarize_model 时的\n"
                "max_tokens 上限。\n\n"
                "控制生成摘要的长度。\n"
                "建议: 400-2000"
            ),
        },
        "history:compression_keep_recent": {
            "label": "压缩尾保条数",
            "location": "Stage 3 → maybe_compress 尾部保护",
            "doc": (
                "历史压缩时，最近 N 条消息完整保留、不进入摘要。\n\n"
                "对应 History.compress_middle 的 keep_last 参数。\n"
                "与 context_pruning.keep_recent (assistant 轮) 是不同概念。\n\n"
                "建议: 4-10"
            ),
        },
        "history:keep_user_messages": {
            "label": "保护全部用户消息",
            "location": "Stage 3 → add_message / maybe_compress 头部保护",
            "doc": (
                "开启: 所有用户消息在裁剪和压缩时均受头部保护。\n"
                "关闭: 仅保护首条用户消息 (及 index 0)。\n\n"
                "位置: History.compress_middle 的 protect_users 参数\n\n"
                "长对话中开启会占用更多上下文预算。"
            ),
        },
        "history:history_summarize_enabled": {
            "label": "历史 AI 压缩开关",
            "location": "Stage 3 → maybe_compress",
            "doc": (
                "控制历史中间段是否用 summarize_model 做 AI 摘要。\n\n"
                "关闭或 AI 失败: 中间段经 build_compress_message 机械压缩\n"
                "(每条留首行预览，受 compress_fallback_chars 字符预算约束)，\n"
                "不会静默丢弃 (head + tail 始终保留)。\n"
                "与 tool_results.summarize_enabled 无关。\n\n"
                "建议: 长对话保持开启"
            ),
        },
        "history:cross_turn_repeat_guard": {
            "label": "跨轮重复守卫",
            "location": "Stage 3 → add_message",
            "doc": (
                "开关: 检测某 agent 新消息与它自己上一轮消息高度相似时\n"
                "记 WARNING 到 gateway.log (可观测)，使跨轮重复自动可见\n"
                "(否则需 leader 手动发现\"X 重复了，没有新信息\")。\n\n"
                "仅记日志，不改写内容——折叠历史会令 agent 误以为上一轮\n"
                "未记全而下轮再解释，反而加重重复。收敛仍交 leader。\n\n"
                "与 tool_loop 的单响应内退化守卫 (_has_contiguous_repeat)\n"
                "互补: 那个管响应内，这个管跨轮。"
            ),
        },
        "history:cross_turn_repeat_ratio": {
            "label": "跨轮重复阈值",
            "location": "Stage 3 → add_message",
            "doc": (
                "agent 新消息与上一轮消息的相似度 ≥ 此值即判为跨轮重复。\n"
                "基于 difflib.SequenceMatcher (whitespace 归一化后)。\n\n"
                "过短消息 (<40字) 不检测，避免\"ok\"/\"done\"误报。\n"
                "值域: 0.0-1.0，默认 0.85\n"
                "建议: 0.80-0.90 (过低误报，过高漏报)"
            ),
        },
        "context_pruning:soft_ratio": {
            "label": "软裁剪触发比例",
            "location": "Stage 4 → prune_messages",
            "doc": (
                "tool_loop 迭代 2+ 时，当\n"
                "estimate_tokens(messages) / context_window_tokens ≥ 此比例\n"
                "时触发裁剪。\n\n"
                "行为: 旧 tool result (>soft_max_chars) 替换为一行摘要。\n\n"
                "建议: 0.3-0.6"
            ),
        },
        "context_pruning:keep_recent": {
            "label": "保护最近 N 轮",
            "location": "Stage 4 → prune_messages",
            "doc": (
                "最近 N 个 assistant turn 内的 tool result 不被裁剪。\n\n"
                "按 assistant 消息计数，不是按 tool 消息条数。\n"
                "建议: 2-6"
            ),
        },
        "context_pruning:soft_max_chars": {
            "label": "软裁剪阈值 (字符)",
            "location": "Stage 4 → context_pruning",
            "doc": (
                "tool result 超过此长度才会被软裁剪。\n"
                "低于此值的 tool result 保持原样。\n\n"
                "建议: 3,000-6,000"
            ),
        },

    # ── Stage 5: tool_limits (工具硬限制) ──────────────────────────
    "tool_limits:read_file_max_chars": {
        "label": "read_file 输出上限 (字符)",
        "location": "Stage 5 → ReadFileTool._MAX_CHARS",
        "doc": (
            "read_file 工具单次返回的字符硬上限。\n\n"
            "超过此值的文件切片会被截断，并提示\n"
            "'Showing lines X-Y of Z. Use offset=Y+1 to continue.'\n"
            "位置: tools/filesystem.py ReadFileTool\n\n"
            "建议: 64,000 (约 1500 行代码)，\n"
            "代码任务可适当调大以减少分页次数"
        ),
    },
    "tool_limits:read_file_default_lines": {
        "label": "read_file 默认行数",
        "location": "Stage 5 → ReadFileTool._DEFAULT_LIMIT",
        "doc": (
            "read_file 不传 limit 参数时的默认行数。\n\n"
            "影响每次读取的窗口大小。值越大单次返回越多，\n"
            "但受 read_file_max_chars 字符上限约束。\n"
            "位置: tools/filesystem.py ReadFileTool\n\n"
            "建议: 300 (默认)，代码任务可调到 500-800"
        ),
    },
    "tool_limits:list_dir_default_max": {
        "label": "list_dir 默认条目上限",
        "location": "Stage 5 → ListDirTool._DEFAULT_MAX",
        "doc": (
            "list_dir 不传 max_entries 时的默认上限。\n\n"
            "超过此值会截断并提示 '(truncated, showing first N of M)'\n"
            "位置: tools/filesystem.py ListDirTool\n\n"
            "建议: 200 (默认)，大型项目可调到 500"
        ),
    },
    "tool_limits:exec_max_timeout": {
        "label": "exec 最大超时 (秒)",
        "location": "Stage 5 → ExecTool._MAX_TIMEOUT",
        "doc": (
            "exec 工具允许的最大超时时间。\n\n"
            "模型传入的 timeout 参数会被 clamp 到此值。\n"
            "同时作为 JSON schema 中 timeout.maximum 约束模型输出。\n"
            "位置: tools/shell.py ExecTool\n\n"
            "建议: 600 (10分钟)，编译/安装任务可适当增加"
        ),
    },
    "tool_limits:exec_max_output": {
        "label": "exec 输出截断 (字符)",
        "location": "Stage 5 → ExecTool._MAX_OUTPUT",
        "doc": (
            "exec 工具返回输出的字符截断上限。\n\n"
            "截断方式: head (前半) + 'N chars truncated' + tail (后半)\n"
            "注意: 此截断在 tool_loop 内发生，独立于\n"
            "tool_results.exec_max_chars (result_processor 阶段)\n"
            "位置: tools/shell.py ExecTool._truncate_output\n\n"
            "建议: 10,000 (默认)"
        ),
    },

    # ── Stage 6: tool_log_preview (工具日志预览) ───────────────────
    "tool_log_preview:web_search": {
        "label": "web_search 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "<previous_tool_calls> 块中 web_search 结果的\n"
            "预览字符上限。\n\n"
            "决定模型在后续轮次能看到多少之前的搜索结果。\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 1,500 (搜索结果需较长预览供回忆)"
        ),
    },
    "tool_log_preview:web_fetch": {
        "label": "web_fetch 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "web_fetch 结果在工具日志中的预览字符上限。\n\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 1,500"
        ),
    },
    "tool_log_preview:read_file": {
        "label": "read_file 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "read_file 结果在工具日志中的预览字符上限。\n\n"
            "代码任务中 read 的内容常是后续 edit 的依据，\n"
            "预览过短会导致模型忘记之前读过什么。\n"
            "旧硬编码值 800，已调高到 1500。\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 1,500 (代码任务可调到 2,000+)"
        ),
    },
    "tool_log_preview:exec": {
        "label": "exec 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "exec 命令输出在工具日志中的预览字符上限。\n\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 500 (命令输出只需结论性预览)"
        ),
    },
    "tool_log_preview:list_dir": {
        "label": "list_dir 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "list_dir 结果在工具日志中的预览字符上限。\n\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 300 (目录列表信息密度低)"
        ),
    },
    "tool_log_preview:chatroom_send": {
        "label": "chatroom_send 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "chatroom_send 在工具日志中的预览字符上限。\n\n"
            "仅显示 (N字) 或 OK，不展开内容。\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 200"
        ),
    },
    "tool_log_preview:wait": {
        "label": "wait 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "wait 工具在工具日志中的预览字符上限。\n\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 200"
        ),
    },
    "tool_log_preview:write_file": {
        "label": "write_file 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "write_file 在工具日志中的预览字符上限。\n\n"
            "旧硬编码值 100，已调高到 300 以保留更多\n"
            "写入路径信息供后续 edit 参考。\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 300"
        ),
    },
    "tool_log_preview:edit_file": {
        "label": "edit_file 日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _PREVIEW_LIMITS",
        "doc": (
            "edit_file 在工具日志中的预览字符上限。\n\n"
            "旧硬编码值 100，已调高到 300。\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 300"
        ),
    },
    "tool_log_preview:_default": {
        "label": "默认日志预览 (字符)",
        "location": "Stage 6 → build_tool_log _DEFAULT_PREVIEW",
        "doc": (
            "未在 _PREVIEW_LIMITS 中显式列出的工具的\n"
            "兜底预览字符上限。\n\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 500"
        ),
    },
    "tool_log_preview:_total_cap": {
        "label": "日志总上限 (字符)",
        "location": "Stage 6 → build_tool_log _TOTAL_CAP",
        "doc": (
            "整个 <previous_tool_calls> 块的字符硬上限。\n\n"
            "超过此值后剩余工具调用只显示 '(还有 N 个工具调用，已省略)'。\n"
            "直接影响每轮 assistant 消息的工具日志体积。\n"
            "位置: context/tool_log.py build_tool_log\n\n"
            "建议: 4,000 (上下文紧张时调到 2,000-3,000)"
        ),
    },
}

