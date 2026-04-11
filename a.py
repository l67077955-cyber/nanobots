#!/usr/bin/env python3
"""
nanobot 自動更新腳本
安全替換 history_to_messages 函數，讓「所有用戶消息」都強制保留
"""

from pathlib import Path
import sys
import shutil
from datetime import datetime

# ==================== 新版本函數 ====================
NEW_FUNCTION = '''    @staticmethod
    def history_to_messages(
        history: list[dict[str, str]],
        current_agent: str = "",
        max_chars: int = 0,
        pin_first_user: bool = True,          # 保留參數，向後相容
        relevant_agents: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert history dicts into LLM API messages.

        新策略（2026.4 推薦）：
        - 強制保留：第1條消息（原始意圖） + 所有 user 消息 + 所有 system 消息
        - 僅在預算不夠時，從中間裁剪 assistant 消息（其他 agent 的發言）
        - 最後從尾部盡量補齊最近的對話，保證時序正確
        """
        def _to_msg(m: dict[str, str]) -> dict[str, Any]:
            sender, content = m["sender"], m["content"]
            if sender == "用户":
                return {"role": "user", "content": content}
            elif sender == "系统":
                return {"role": "system", "content": content}
            else:
                return {
                    "role": "assistant",
                    "content": f"{sender}: {content}",
                    "name": sender.replace(" ", "_"),
                }

        # 1. 先做 relevant_agents 過濾
        filtered = history
        if relevant_agents is not None:
            filtered = [
                m for m in history
                if m["sender"] in ("用户", "系统") or m["sender"] in relevant_agents
            ]

        msgs_full = [_to_msg(m) for m in filtered]
        if not max_chars or not msgs_full:
            return msgs_full

        # 2. 強制保留的核心消息（全部 user + system + 第一條消息）
        critical: list[dict[str, Any]] = []
        user_system_indices = set()

        for i, m in enumerate(msgs_full):
            if m["role"] in ("user", "system") or i == 0:   # 第1條永遠保留
                critical.append(m)
                user_system_indices.add(i)

        # 3. 計算已用字數
        used_chars = sum(len(m.get("content", "")) for m in critical)
        budget = max_chars - used_chars

        # 4. 從尾部往回補 assistant 消息（保持最新對話）
        tail: list[dict[str, Any]] = []
        for m in reversed(msgs_full):
            if m in critical:          # 已經在 critical 裡的不重複加
                continue
            c = len(m.get("content", ""))
            if budget - c < 0:
                break
            tail.insert(0, m)
            budget -= c

        # 5. 重建最終列表（保持原始時序）
        result: list[dict[str, Any]] = []
        added = set()

        for m in msgs_full:
            if m in critical or m in tail:
                # 避免重複（雖然理論上不會）
                if id(m) not in added:   # 用 id 判重更快
                    result.append(m)
                    added.add(id(m))

        # 6. 如果有裁剪，插入省略提示（放在 critical 之後）
        skipped = len(msgs_full) - len(result)
        if skipped > 0:
            result.insert(len(critical), {   # 插在核心消息之後
                "role": "system",
                "content": f"[...{skipped} 條歷史消息已省略以節省上下文...]",
            })

        return result
'''

# ==================== 舊版本函數（用來精準替換） ====================
OLD_FUNCTION = '''    @staticmethod
    def history_to_messages(
        history: list[dict[str, str]],
        current_agent: str = "",
        max_chars: int = 0,
        pin_first_user: bool = True,
        relevant_agents: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert history dicts into LLM API messages.

        When max_chars > 0, applies a budget strategy:
        - Pins the first user message (preserves original intent)
        - Fills remaining budget from the tail (most recent messages)
        - Inserts a system placeholder if any middle messages were skipped

        When relevant_agents is set, other agents' messages are filtered out
        (用户 and 系统 messages are always kept). Used in broadcast mode so
        each agent only sees its own prior turns rather than every agent's output.
        """
        def _to_msg(m: dict[str, str]) -> dict[str, Any]:
            sender, content = m["sender"], m["content"]
            if sender == "用户":
                return {"role": "user", "content": content}
            elif sender == "系统":
                return {"role": "system", "content": content}
            else:
                return {
                    "role": "assistant",
                    "content": f"{sender}: {content}",
                    "name": sender.replace(" ", "_"),
                }

        # Apply agent filter before any budget logic
        filtered = history
        if relevant_agents is not None:
            filtered = [
                m for m in history
                if m["sender"] in ("用户", "系统") or m["sender"] in relevant_agents
            ]

        msgs_full = [_to_msg(m) for m in filtered]

        if not max_chars or not msgs_full:
            return msgs_full

        # Find first user message to pin
        pinned: list[dict[str, Any]] = []
        rest_start = 0
        if pin_first_user:
            for i, m in enumerate(msgs_full):
                if m["role"] == "user":
                    pinned = [m]
                    rest_start = i + 1
                    break

        # Fill from tail within remaining budget
        pinned_chars = sum(len(m.get("content", "")) for m in pinned)
        budget = max_chars - pinned_chars
        tail: list[dict[str, Any]] = []
        for m in reversed(msgs_full[rest_start:]):
            c = len(m.get("content", ""))
            if budget - c < 0:
                break
            tail.insert(0, m)
            budget -= c

        skipped = len(msgs_full) - rest_start - len(tail)
        result = list(pinned)
        if skipped > 0:
            result.append({
                "role": "system",
                "content": f"[...{skipped} 条历史消息已省略以节省上下文...]",
            })
        result.extend(tail)
        return result
'''

def main():
    # 自動尋找檔案
    possible_paths = [
        Path("nanobot/groupchat/prompt_builder.py"),
        Path(__file__).parent / "nanobot/groupchat/prompt_builder.py",
        Path.home() / "nanobot" / "nanobot/groupchat/prompt_builder.py",
    ]

    target_file = None
    for p in possible_paths:
        if p.exists():
            target_file = p
            break

    if not target_file:
        print("❌ 找不到 prompt_builder.py")
        print("請手動指定路徑，例如：")
        print("   python update_history_to_messages.py /你的路徑/nanobot/groupchat/prompt_builder.py")
        sys.exit(1)

    print(f"✅ 找到檔案：{target_file}")

    # 1. 備份
    backup = target_file.with_suffix(".py.bak")
    if not backup.exists():
        shutil.copy2(target_file, backup)
        print(f"📦 已備份 → {backup}")

    # 2. 讀取原檔案
    content = target_file.read_text(encoding="utf-8")

    # 3. 替換
    if OLD_FUNCTION not in content:
        print("⚠️  舊函數已不存在，可能已經更新過")
        sys.exit(0)

    new_content = content.replace(OLD_FUNCTION, NEW_FUNCTION)

    # 4. 寫回
    target_file.write_text(new_content, encoding="utf-8")

    print("🎉 替換成功！")
    print("   history_to_messages 現在會保留**所有用戶消息**")
    print(f"   備份檔案：{backup}")
    print("\n你可以直接重啟 nanobot 測試了～")

if __name__ == "__main__":
    main()