cat > fix_single_agent.py << 'EOF'
#!/usr/bin/env python3
"""
nanobot 第三版修復腳本 - 只修單 Agent 模式
"""
from pathlib import Path
import sys
import shutil

NEW_BUILD = '''    def build_single_agent_messages(
        self,
        agent_name: str,
        *,
        registry: dict[str, dict],
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
    ) -> list[dict[str, Any]]:
        """Build complete messages list for a single-agent LLM call.

        【重要更新 2026.4】
        - 不再繞過 history_to_messages
        - 直接把真實 history 傳入 build_agent_prompt，讓智能裁剪生效
        - 單 Agent 模式現在也會保留所有 user 消息 + 從尾部補齊
        """
        from nanobot.utils.helpers import build_runtime_context, build_user_content

        # Build system prompt components + 經過智能裁剪的 history
        messages = self.build_agent_prompt(
            agent_name,
            registry=registry,
            active_agents=[agent_name],
            history=history,           # ← 關鍵修改：傳入真實歷史
            leader=None,
            round_num=0,
        )

        # Build runtime context + user content
        runtime_ctx = build_runtime_context(channel, chat_id)
        user_content = build_user_content(current_message, media)

        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        messages.append({"role": current_role, "content": merged})
        return messages
'''

# 找檔案
possible = [
    Path("nanobot/groupchat/prompt_builder.py"),
    Path.home() / "nanobot" / "nanobot/groupchat/prompt_builder.py",
]
target = None
for p in possible:
    if p.exists():
        target = p
        break

if not target:
    print("❌ 找不到 prompt_builder.py")
    sys.exit(1)

print(f"✅ 找到檔案：{target}")

# 備份
backup = target.with_suffix(".py.bak3")
shutil.copy2(target, backup)
print(f"📦 已備份 → {backup}")

content = target.read_text(encoding="utf-8")

# 找舊函數並替換（用較寬鬆的關鍵區塊）
if "history=[],  # history handled below as raw LLM messages" in content:
    # 原始版本
    old_part = '''        # Build system prompt components
        messages = self.build_agent_prompt(
            agent_name,
            registry=registry,
            active_agents=[agent_name],
            history=[],  # history handled below as raw LLM messages
            leader=None,
            round_num=0,
        )

        # Append conversation history (raw LLM message dicts, not group chat format)
        messages.extend(history)
'''
    new_part = '''        # Build system prompt components + 經過智能裁剪的 history
        messages = self.build_agent_prompt(
            agent_name,
            registry=registry,
            active_agents=[agent_name],
            history=history,           # ← 關鍵修改：傳入真實歷史
            leader=None,
            round_num=0,
        )
'''
    content = content.replace(old_part, new_part)
    print("🔄 已成功替換 build_single_agent_messages（使用原始錨點）")

elif "history handled below as raw LLM messages" in content:
    # 可能已被部分修改過
    content = content.replace(
        'history=[],  # history handled below as raw LLM messages',
        'history=history,           # ← 關鍵修改：傳入真實歷史'
    )
    print("🔄 已替換關鍵一行（history=[] → history=history）")
else:
    print("⚠️  找不到可替換的舊文字，請把下面這段貼給我：")
    print("   sed -n '/def build_single_agent_messages/,/^    def /p' nanobot/groupchat/prompt_builder.py")
    sys.exit(1)

target.write_text(content, encoding="utf-8")

print("\n🎉 修復完成！")
print("   單 Agent 模式現在也走智能歷史裁剪")
print(f"   備份：{backup}")
print("\n請執行：")
print("   python -m nanobot   # 或你原本的重啟指令")
print("然後測試單 Agent 模式～")
EOF

python3 fix_single_agent.py