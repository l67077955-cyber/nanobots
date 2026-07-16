#!/usr/bin/env python3
"""Migrate from 3 separate JSON files + code constants → single prompt_manifest.json.

Reads:
  ~/.nanobot/prompt_order.json
  ~/.nanobot/prompt_visibility.json
  ~/.nanobot/custom_prompt_labels.json
  ~/.nanobot/prompts/*.md
  Code constants from prompt_builder.py (DEFAULT_PROMPT_ORDER, COMPONENT_LABELS, etc.)

Writes:
  ~/.nanobot/prompt_manifest.json
"""

import json
from pathlib import Path

HOME = Path.home()
NANOBOT = HOME / ".nanobot"
PROMPTS_DIR = NANOBOT / "prompts"

# ── Read existing JSONs ──

def read_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}

order_data = read_json(NANOBOT / "prompt_order.json")
visibility_data = read_json(NANOBOT / "prompt_visibility.json")
labels_data = read_json(NANOBOT / "custom_prompt_labels.json")

# ── Code constants (hardcoded fallback defaults) ──

DEFAULT_PROMPT_ORDER = [
    "main_prompt", "group_context", "persona", "memory",
    "tool_instructions", "skills", "broadcast_hint", "examples",
    "history", "instructions", "leader_prompt", "group_nudge",
]

COMPONENT_LABELS: dict[str, str] = {
    "main_prompt": "主提示 (main_prompt)",
    "group_context": "群聊上下文 (group_context)",
    "persona": "人设/SOUL (persona)",
    "memory": "长期记忆 (memory)",
    "tool_instructions": "工具指令 (tool_instructions)",
    "skills": "技能列表 (skills)",
    "broadcast_hint": "广播协调 (broadcast_hint)",
    "examples": "示例对话 (examples)",
    "history": "聊天记录 (history)",
    "instructions": "后置指令 (instructions)",
    "leader_prompt": "领袖指令 (leader_prompt)",
    "group_nudge": "群聊规范 (group_nudge)",
}

GLOBAL_EDITABLE: set[str] = {
    "main_prompt", "group_context", "tool_instructions", "skills", "memory",
    "broadcast_hint", "examples", "instructions", "leader_prompt", "group_nudge",
}
AGENT_EDITABLE = {"persona"}

# ── Determine effective order ──

order_list = order_data.get("default", DEFAULT_PROMPT_ORDER)

# ── Build manifest ──

# Collect all known component keys
all_keys = list(dict.fromkeys(
    list(order_list) +
    list(COMPONENT_LABELS.keys()) +
    list(visibility_data.keys()) +
    list(labels_data.keys())
))

# Check which .md files exist
existing_md = {f.stem for f in PROMPTS_DIR.glob("*.md") if f.stem != "leader_prompt"}

components = {}
for idx, key in enumerate(all_keys):
    # Label: custom labels override code defaults
    label = labels_data.get(key, COMPONENT_LABELS.get(key, key))

    # Visibility
    visibility = visibility_data.get(key, "leader" if key == "leader_prompt" else "all")

    # Editable_by
    if key in AGENT_EDITABLE:
        editable_by = "agent"
    elif key in GLOBAL_EDITABLE:
        editable_by = "global"
    else:
        editable_by = "none"

    # Source path: check if .md file exists
    source_path = None
    if key in existing_md:
        source_path = f"prompts/{key}.md"

    components[key] = {
        "order": idx,
        "visibility": visibility,
        "label": label,
        "source_path": source_path,
        "editable_by": editable_by,
        "resolver": None,
    }

manifest = {
    "version": 1,
    "components": components,
}

# ── Write manifest ──

manifest_path = NANOBOT / "prompt_manifest.json"
manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2)
)

print(f"✅ Manifest written: {manifest_path}")
print(f"   Components: {len(components)}")
print(f"   Keys: {', '.join(all_keys)}")
