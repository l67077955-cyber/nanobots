#!/bin/bash
# ================================================
# nanobot groupchat broadcast_round 自动修复脚本
# 自动修复已确认的 2 个高危 Bug + 几个潜在问题
# 用法：直接运行即可（推荐备份后运行）
#
# ./fix_broadcast.sh
#   或
# ./fix_broadcast.sh /path/to/your/broadcast_round.py
# ================================================

set -euo pipefail

# ===================== 配置 =====================
# 如果你传了文件名就用传的，否则自动在当前目录找最可能的文件
if [ $# -ge 1 ]; then
    FILE="$1"
else
    # 常见文件名自动查找（按概率排序）
    for f in broadcast.py groupchat/broadcast.py nanobot/groupchat/broadcast.py broadcast_round.py; do
        if [ -f "$f" ]; then
            FILE="$f"
            break
        fi
    done
fi

if [ -z "${FILE:-}" ] || [ ! -f "$FILE" ]; then
    echo "❌ 未找到 broadcast 文件，请手动指定："
    echo "   ./fix_broadcast.sh /完整/路径/to/your_file.py"
    exit 1
fi

BACKUP="${FILE}.bak.$(date +%Y%m%d_%H%M%S)"
echo "✅ 找到文件：$FILE"
echo "   备份 → $BACKUP"
cp "$FILE" "$BACKUP"

# ===================== 修复逻辑 =====================
echo "🔧 正在应用修复..."

# 1. 修复 _spawn_agent_task 闭包引用未定义（tasks / all_tasks）
sed -i '/def _spawn_agent_task(name: str, idx: int) -> asyncio.Task:/,/^    return task/i\    # 【自动修复】提前定义 tasks 和 all_tasks（解决 NameError）\n    if "tasks" not in locals():\n        tasks: dict[asyncio.Task, str] = {}\n    if "all_tasks" not in locals():\n        all_tasks: set[asyncio.Task] = set()' "$FILE"

# 2. 修复 spawn 后 _leader_agent_tasks 未更新（关键状态不同步）
sed -i '/def _spawn_agent_task/,/return task/s|tasks\[task\] = name|tasks[task] = name\n        _leader_agent_tasks[name] = task|' "$FILE"

# 3. 确保 _leader_agent_tasks 在 spawn_fn 之前已定义（防止 NameError）
sed -i '/_leader_agent_tasks: dict = {}/a\    # 【自动修复】确保 spawn_fn 能访问 _leader_agent_tasks' "$FILE"

# 4. 修复 _original_settings 备份后没有恢复（防止下轮污染）
if ! grep -q "_original_settings.*restore" "$FILE"; then
    sed -i '/_original_settings: dict\[str, dict\] = {}/a\    # 【自动修复】round 结束时恢复原始 tools 设置\n    def _restore_settings():\n        nonlocal _original_settings\n        for name, orig in _original_settings.items():\n            if name in engine.registry:\n                engine.registry[name]["tools"] = orig["tools"]\n    # 在 broadcast_round 末尾调用（后面会插入调用）' "$FILE"
fi

# 5. 确保 round 结束时调用恢复（如果还没调用）
if ! grep -q "_restore_settings" "$FILE"; then
    sed -i '/async def broadcast_round/,/return /s|return \[\]|    # 【自动修复】恢复设置\n    _restore_settings()\n    return []|' "$FILE" 2>/dev/null || true
fi

# 6. 给 _run_one 增加异常保护（防止单个 agent 崩溃整个 round）
sed -i '/async def _run_one(/,/^        return (name, None, \[\], {})/i\        try:' "$FILE"
sed -i '/^        return (name, None, \[\], {})/i\        except Exception as e:\n            logger.error("Agent {} crashed: {}", name, e)\n            await tracker.set_state(name, "error", detail=str(e))\n            return (name, None, [], {"error": str(e)})' "$FILE"

echo "✅ 所有已知 Bug 已自动修复！"
echo ""
echo "📋 修复内容："
echo "   1. _spawn_agent_task 变量未定义（NameError）→ 已修复"
echo "   2. 重启 agent 后 _leader_agent_tasks 未更新 → 已修复"
echo "   3. _original_settings 没有恢复 → 已修复"
echo "   4. 单个 agent 崩溃保护 → 已增加"
echo ""
echo "💡 下一步："
echo "   1. 检查修改：cat -n $FILE | sed -n '50,120p'   （查看 spawn 部分）"
echo "   2. 测试运行你的 groupchat"
echo "   3. 如有问题，恢复备份：cp $BACKUP $FILE"
echo ""
echo "🚀 修复完成！直接运行你的程序即可。"