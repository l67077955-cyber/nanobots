#!/usr/bin/env python3
"""Regenerate cb_agents.py from original monolithic callbacks.py."""
from __future__ import annotations

import re
from pathlib import Path

ORIG = Path(__file__).resolve().parent.parent / ".callbacks_original.py"
OUT = Path(__file__).resolve().parent.parent / "nanobot/channels/telegram/callbacks/cb_agents.py"

# Line ranges (1-based) of agent-related top-level elif blocks in original _on_callback
RANGES = [
    (111, 318),   # add..ef
    (351, 613),   # ef_re..srr
    (1124, 1372), # sl..ord
]

HEADER = '''"""Telegram agent/group/hyperparam callbacks."""
from __future__ import annotations

import json
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from loguru import logger


class AgentCallbackMixin:
    async def _dispatch_agents(self, query, data: str, chat_id: str) -> bool:
'''

FOOTER = '''
        return False
'''


def transform_block(lines: list[str]) -> str:
    out: list[str] = []
    for j, line in enumerate(lines):
        if j == 0:
            line = line.replace("            elif ", "        if ", 1).replace("            if ", "        if ", 1)
        elif line.startswith("            "):
            line = line[4:]
        out.append(line)

    # Normalize bare `return` at block end to `return True` for unmatched preset branches
    for k, line in enumerate(out):
        if line.strip() == "return" and k > 0:
            prev = out[k - 1].strip()
            if prev and not prev.endswith(":"):
                out[k] = line.replace("return", "return True")

    if not any(l.strip() == "return True" for l in out[-5:]):
        out.append("")
        out.append("        return True")
    return "\n".join(out)


def main() -> None:
    all_lines = ORIG.read_text().splitlines()
    blocks: list[str] = []
    for start, end in RANGES:
        chunk = all_lines[start - 1 : end]
        blocks.append(transform_block(chunk))

    body = "\n\n".join(blocks)
    OUT.write_text(HEADER + body + FOOTER)
    print(f"Wrote {OUT} ({len(blocks)} blocks, {len(OUT.read_text().splitlines())} lines)")


if __name__ == "__main__":
    main()