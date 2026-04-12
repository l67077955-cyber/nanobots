"""Memory Palace Tool — structured, persistent long-term memory for agents.

Architecture: Wing → Hall → Room → list[memory]
Persisted as a single JSON file; loaded once at construction and written on
every store/clear operation.  All operations are synchronous internally but
wrapped in an async execute() to satisfy the Tool interface.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool


class MemoryPalaceTool(Tool):
    """Persistent, structured long-term memory using the Method of Loci.

    Storage layout (JSON file)::

        {
          "<wing>": {
            "<hall>": {
              "<room>": [
                {"content": "...", "timestamp": 1234567890.0, "metadata": {...}},
                ...
              ]
            }
          }
        }

    Supported actions
    -----------------
    store  : write a memory into Wing/Hall/Room
    search : keyword search across all rooms (returns top-k by recency)
    list   : show the current palace structure (wing / hall / room counts)
    clear  : wipe the entire palace (destructive — adds confirmation guard)
    delete : remove a single room, hall, or wing by path
    """

    def __init__(self, storage_path: str = "./memory_palace") -> None:
        self._storage_dir = Path(storage_path)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._palace_file = self._storage_dir / "palace.json"
        # Wing → Hall → Room → list[dict]
        self._palace: dict[str, dict[str, dict[str, list[dict]]]] = self._load()

    # ── Tool identity ──────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "memory_palace"

    @property
    def description(self) -> str:
        return (
            "Manage persistent long-term memory using the Memory Palace (Method of Loci). "
            "Organises memories in a Wing → Hall → Room hierarchy that survives across "
            "sessions and rounds. Actions: store, search, list, clear, delete."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["store", "search", "list", "clear", "delete"],
                    "description": (
                        "Operation to perform: "
                        "'store' — save a memory; "
                        "'search' — keyword search; "
                        "'list' — show palace structure; "
                        "'clear' — wipe everything (requires confirm=true); "
                        "'delete' — remove a wing/hall/room path."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "[store] The text content to memorise.",
                },
                "wing": {
                    "type": "string",
                    "description": (
                        "[store/delete] Top-level category, e.g. 'project', 'user_prefs'. "
                        "Defaults to 'default'."
                    ),
                },
                "hall": {
                    "type": "string",
                    "description": (
                        "[store/delete] Mid-level grouping within a wing, e.g. 'decisions', '2026-04'. "
                        "Defaults to 'general'."
                    ),
                },
                "room": {
                    "type": "string",
                    "description": (
                        "[store/delete] Fine-grained slot within a hall, e.g. 'api_design'. "
                        "Defaults to 'main'."
                    ),
                },
                "metadata": {
                    "type": "object",
                    "description": "[store] Optional key-value metadata attached to the memory.",
                },
                "query": {
                    "type": "string",
                    "description": "[search] Keyword(s) to search for across all rooms.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "[search] Maximum number of results to return. Default 5.",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "[clear] Must be true to actually wipe the palace.",
                },
            },
            "required": ["action"],
        }

    # ── Public entry point ─────────────────────────────────────────────────

    async def execute(  # noqa: PLR0911
        self,
        action: str,
        content: str = "",
        wing: str = "default",
        hall: str = "general",
        room: str = "main",
        metadata: dict | None = None,
        query: str = "",
        top_k: int = 5,
        confirm: bool = False,
        **_: Any,
    ) -> str:
        if action == "store":
            return self._store(content, wing, hall, room, metadata)
        if action == "search":
            return self._search(query, top_k)
        if action == "list":
            return self._list_structure()
        if action == "clear":
            return self._clear(confirm)
        if action == "delete":
            return self._delete(wing, hall, room)
        return (
            f"Unknown action '{action}'. "
            "Supported: store, search, list, clear, delete."
        )

    # ── Operations ─────────────────────────────────────────────────────────

    def _store(
        self,
        content: str,
        wing: str,
        hall: str,
        room: str,
        metadata: dict | None,
    ) -> str:
        if not content.strip():
            return "Error: 'content' must not be empty."

        wing = wing.strip() or "default"
        hall = hall.strip() or "general"
        room = room.strip() or "main"

        self._palace.setdefault(wing, {})
        self._palace[wing].setdefault(hall, {})
        self._palace[wing][hall].setdefault(room, [])

        entry: dict[str, Any] = {
            "content": content,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self._palace[wing][hall][room].append(entry)
        self._save()

        total = len(self._palace[wing][hall][room])
        return (
            f"✅ Memory stored.\n"
            f"  Location : {wing} / {hall} / {room}\n"
            f"  Length   : {len(content)} chars\n"
            f"  Room now : {total} entr{'y' if total == 1 else 'ies'}"
        )

    def _search(self, query: str, top_k: int) -> str:
        if not query.strip():
            return "Error: 'query' must not be empty."

        q = query.lower()
        hits: list[dict] = []

        for wing, halls in self._palace.items():
            for hall, rooms in halls.items():
                for room, memories in rooms.items():
                    for mem in memories:
                        if q in mem["content"].lower():
                            preview = mem["content"]
                            if len(preview) > 400:
                                preview = preview[:400] + "…"
                            hits.append({
                                "path": f"{wing}/{hall}/{room}",
                                "preview": preview,
                                "ts": mem["timestamp"],
                            })

        if not hits:
            return f"No memories found matching '{query}'."

        hits.sort(key=lambda h: h["ts"], reverse=True)
        hits = hits[:max(1, int(top_k))]

        lines = [f"🔍 {len(hits)} result(s) for '{query}' (most recent first):"]
        for i, h in enumerate(hits, 1):
            ts_str = _fmt_ts(h["ts"])
            lines.append(f"\n{i}. [{h['path']}]  {ts_str}\n{h['preview']}")
        return "\n".join(lines)

    def _list_structure(self) -> str:
        if not self._palace:
            return "The memory palace is empty."

        lines = ["🧠 Memory Palace structure:"]
        for wing, halls in self._palace.items():
            wing_total = sum(
                len(mems)
                for rooms in halls.values()
                for mems in rooms.values()
            )
            lines.append(f"  ├─ Wing: {wing}  ({wing_total} total memories)")
            for hall, rooms in halls.items():
                hall_total = sum(len(m) for m in rooms.values())
                lines.append(f"  │   ├─ Hall: {hall}  ({hall_total})")
                for room, mems in rooms.items():
                    lines.append(f"  │   │   └─ Room: {room}  ({len(mems)} entries)")
        return "\n".join(lines)

    def _clear(self, confirm: bool) -> str:
        if not confirm:
            return (
                "⚠️  This will permanently delete ALL memories. "
                "Call again with confirm=true to proceed."
            )
        count = sum(
            len(mems)
            for halls in self._palace.values()
            for rooms in halls.values()
            for mems in rooms.values()
        )
        self._palace.clear()
        self._save()
        return f"🗑️ Memory palace cleared. {count} memories deleted."

    def _delete(self, wing: str, hall: str, room: str) -> str:
        """Remove a room (if hall+room given), a hall (if only hall given), or a wing."""
        # If room is non-default, delete just the room
        if wing in self._palace:
            if hall in self._palace[wing]:
                if room != "main" and room in self._palace[wing][hall]:
                    count = len(self._palace[wing][hall].pop(room))
                    if not self._palace[wing][hall]:
                        del self._palace[wing][hall]
                    if not self._palace[wing]:
                        del self._palace[wing]
                    self._save()
                    return f"Deleted room '{wing}/{hall}/{room}' ({count} memories)."
                elif hall != "general":
                    count = sum(len(m) for m in self._palace[wing].pop(hall).values())
                    if not self._palace[wing]:
                        del self._palace[wing]
                    self._save()
                    return f"Deleted hall '{wing}/{hall}' ({count} memories)."
            elif wing != "default":
                count = sum(
                    len(mems)
                    for rooms in self._palace.pop(wing).values()
                    for mems in rooms.values()
                )
                self._save()
                return f"Deleted wing '{wing}' ({count} memories)."
        return f"Path '{wing}/{hall}/{room}' not found in the palace."

    # ── Persistence ────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self._palace_file.exists():
            try:
                return json.loads(self._palace_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("MemoryPalace: failed to load {}: {}", self._palace_file, exc)
        return {}

    def _save(self) -> None:
        try:
            self._palace_file.write_text(
                json.dumps(self._palace, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("MemoryPalace: failed to save {}: {}", self._palace_file, exc)


# ── Helpers ────────────────────────────────────────────────────────────────

def _fmt_ts(ts: float) -> str:
    """Format a Unix timestamp as a compact local-time string."""
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M")
