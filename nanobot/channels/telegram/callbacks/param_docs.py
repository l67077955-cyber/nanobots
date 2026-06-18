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
            "location": "全局 → agent loop 入口处",
            "doc": (
                "工具返回的原始输出在进入任何后处理之前的"
                "字符硬上限。超过即截断。\n\n"
                "位置: agent/loop.py 初始化时读取\n"
                "时机: 最早的一刀 — 在 Stage 1 分工具截断之前\n"
                "截断方式: 保留首尾各一半，中间标记 truncated\n\n"
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
            "location": "Stage 1 → 网页抓取输出",
            "doc": (
                "web_fetch 工具 (URL 抓取) 返回内容的最大字符数。\n\n"
                "场景: 抓取网页/API 的 HTML→Markdown 转换结果\n"
                "截断方式: head + truncated 标记 + tail\n"
                "特点: 网页内容通常含大量导航/页脚噪音，"
                "适当降低可提升信噪比\n\n"
                "建议: 一般 8,000-15,000 即可覆盖正文"
            ),
        },
        "tool_results:web_search_max_chars": {
            "label": "web_search 截断 (字符)",
            "location": "Stage 1 → 搜索结果输出",
            "doc": (
                "web_search 工具返回的搜索结果最大字符数。\n\n"
                "场景: 搜索引擎结果摘要列表\n"
                "截断方式: head + truncated 标记 + tail\n"
                "特点: 搜索结果结构化程度高、信息密度大，"
                "通常比网页内容更紧凑\n\n"
                "建议: 5,000-10,000 即可包含足够条目"
            ),
        },
        "tool_results:summarize_threshold": {
            "label": "AI 总结触发阈值 (字符)",
            "location": "Stage 2 → 总结器入口判断",
            "doc": (
                "工具输出超过此字符数时，调用小模型提取关键信息。\n\n"
                "流程: 原始输出 → LLM 提取要点 → 压缩结果注入上下文\n"
                "失败兜底: head+tail 截断 (summarizer.py)\n"
                "模型: 使用 summarize_model 指定的轻量模型\n\n"
                "关键约束: 必须 < exec/web_fetch/web_search_max_chars\n"
                "  否则输出在 Stage 1 已被截断到阈值以下，"
                "总结器永远不触发\n\n"
                "建议: 设为各工具截断值的 60-80%"
            ),
        },
        "tool_results:summarize_enabled": {
            "label": "AI 总结开关",
            "location": "Stage 2 → 总结器启用/禁用",
            "doc": (
                "控制是否启用 LLM 自动总结工具输出。\n\n"
                "开启: 超过阈值的工具结果用小模型压缩\n"
                "关闭: 跳过总结，仅依靠 Stage 1 截断"
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
            "location": "Stage 2 → broadcast tool_loop",
            "doc": (
                "广播模式下 tool_loop 的 result_max_chars 参数。\n\n"
                "控制每个工具结果注入 LLM 上下文前的最大字符数。\n"
                "超过此值会触发 AI 总结或截断。\n"
                "广播模式通常需要更大的值，因为多 agent 并行。\n\n"
                "建议: 15,000-30,000"
            ),
        },
        "tool_results:direct_result_max_chars": {
            "label": "直接模式 result_max_chars",
            "location": "Stage 2 → direct/serial tool_loop",
            "doc": (
                "直接对话/串行模式下 tool_loop 的 result_max_chars。\n\n"
                "控制每个工具结果注入 LLM 上下文前的最大字符数。\n"
                "超过此值会触发 AI 总结或截断。\n\n"
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
        "history:compress_max_summary_tokens": {
            "label": "历史压缩摘要长度 (tokens)",
            "location": "Stage 3 → 历史压缩",
            "doc": (
                "压缩历史时，摘要模型的最大输出 token 数。\n\n"
                "控制生成摘要的长度上限。\n"
                "建议: 400-800"
            ),
        },
        "context_pruning:soft_ratio": {
            "label": "软裁剪触发比例",
            "location": "Stage 4 → context_pruning",
            "doc": (
                "tool_loop 迭代 2+ 时，当上下文字符数超过\n"
                "context_window_tokens × CHARS_PER_TOKEN × 此比例\n"
                "时触发软裁剪。\n\n"
                "软裁剪: 旧 tool result 截断为 head+tail，"
                "中间部分提取关键事实。\n\n"
                "建议: 0.2-0.4"
            ),
        },
        "context_pruning:keep_recent": {
            "label": "保护最近 N 轮",
            "location": "Stage 4 → context_pruning",
            "doc": (
                "最近 N 个 assistant turn 的 tool result 不被裁剪。\n\n"
                "保护最近的工具结果，确保模型能引用最新数据。\n"
                "建议: 2-5"
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
}

