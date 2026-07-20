"""Memory Palace Tool — Official MemPalace library adapter for nanobot agents.

Wraps the official MemPalace package (https://github.com/Alec-Dieken/MemPalace)
as a nanobot Tool, providing the full feature set:

  Architecture : Wing → Room hierarchy in ChromaDB
  Halls        : hall_facts, hall_events, hall_discoveries, hall_preferences, hall_advice, hall_diary
  Drawers      : verbatim content stored via tool_add_drawer
  Tunnels      : cross-wing room connections via palace_graph
  KG           : SQLite entity-relationship knowledge graph
  Layers       : L0 Identity + L1 Essential Story wake-up (~600-900 tokens)

Actions
-------
  wake_up        — L0 + L1: identity + essential story (~600-900 tokens)
  store          — file verbatim content into wing/room (tool_add_drawer)
  search         — semantic search, optional wing/room filter (tool_search)
  recall         — L2 on-demand: all drawers in a wing/room
  status         — palace overview: total drawers, wings, rooms
  list_wings     — all wings with drawer counts
  list_rooms     — rooms within a wing (or all)
  taxonomy       — full wing → room → count tree
  delete_drawer  — remove a single drawer by ID
  traverse       — walk palace graph from a room (tunnels)
  find_tunnels   — rooms bridging two wings
  kg_query       — query entity relationships from knowledge graph
  kg_add         — add subject→predicate→object fact
  kg_invalidate  — mark a fact as ended
  kg_timeline    — chronological timeline of facts
  diary_write    — write agent diary entry
  diary_read     — read agent's recent diary entries
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from loguru import logger

from nanobot.tools.base import Tool

# Serialise mempalace library calls (ChromaDB client cache is not built for
# concurrent construction) while keeping the asyncio event loop free — every
# sync library call is dispatched via asyncio.to_thread(_locked, ...).
_MP_LOCK = threading.Lock()


def _locked(fn, *args, **kwargs):
    with _MP_LOCK:
        return fn(*args, **kwargs)


# ── Palace path: use nanobot storage area by default ──────────────────────────
_PALACE_PATH = os.environ.get(
    "MEMPALACE_PALACE_PATH",
    os.path.expanduser("~/.nanobot/mempalace/palace"),
)
# Ensure the directory exists and point the official library to it
os.environ.setdefault("MEMPALACE_PALACE_PATH", _PALACE_PATH)
os.makedirs(_PALACE_PATH, exist_ok=True)


def _import_official():
    """Lazy import of official MemPalace functions."""
    try:
        from mempalace.mcp_server import (  # noqa: PLC0415
            tool_add_drawer,
            tool_delete_drawer,
            tool_status,
            tool_list_wings,
            tool_list_rooms,
            tool_get_taxonomy,
            tool_search,
            tool_traverse_graph,
            tool_find_tunnels,
            tool_graph_stats,
            tool_kg_query,
            tool_kg_add,
            tool_kg_invalidate,
            tool_kg_timeline,
            tool_kg_stats,
            tool_diary_write,
            tool_diary_read,
        )
        from mempalace.layers import MemoryStack  # noqa: PLC0415
        return {
            "add_drawer": tool_add_drawer,
            "delete_drawer": tool_delete_drawer,
            "status": tool_status,
            "list_wings": tool_list_wings,
            "list_rooms": tool_list_rooms,
            "taxonomy": tool_get_taxonomy,
            "search": tool_search,
            "traverse": tool_traverse_graph,
            "find_tunnels": tool_find_tunnels,
            "graph_stats": tool_graph_stats,
            "kg_query": tool_kg_query,
            "kg_add": tool_kg_add,
            "kg_invalidate": tool_kg_invalidate,
            "kg_timeline": tool_kg_timeline,
            "kg_stats": tool_kg_stats,
            "diary_write": tool_diary_write,
            "diary_read": tool_diary_read,
            "MemoryStack": MemoryStack,
        }
    except ImportError as e:
        raise ImportError(
            f"Official MemPalace library not installed: {e}. "
            "Run: pip install -e /root/.nanobot/workspace/mempalace --break-system-packages"
        ) from e


def _fmt(result: Any) -> str:
    """Format a dict/list result as a compact string for LLM consumption."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and "error" in result:
        return f"❌ Error: {result['error']}" + (f"\n💡 {result.get('hint', '')}" if result.get("hint") else "")
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


class MemoryPalaceTool(Tool):
    """Persistent long-term memory using the official MemPalace library.

    Organises memories as Wing → Room → Drawer hierarchy backed by ChromaDB.
    Supports semantic search, knowledge graph (entity-relationship), agent diary,
    and Tunnel-based cross-wing graph traversal.
    """

    # ── Tool identity ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "memory_palace"

    @property
    def description(self) -> str:
        return (
            "Persistent long-term memory palace (official MemPalace). "
            "Organises memories as Wing → Room → Drawer with ChromaDB semantic search, "
            "cross-wing Tunnel graph, entity knowledge graph, and agent diary. "
            "Actions: wake_up, store, search, recall, status, list_wings, list_rooms, "
            "taxonomy, delete_drawer, traverse, find_tunnels, "
            "kg_query, kg_add, kg_invalidate, kg_timeline, diary_write, diary_read."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "wake_up",
                        "store",
                        "search",
                        "recall",
                        "status",
                        "list_wings",
                        "list_rooms",
                        "taxonomy",
                        "delete_drawer",
                        "traverse",
                        "find_tunnels",
                        "kg_query",
                        "kg_add",
                        "kg_invalidate",
                        "kg_timeline",
                        "diary_write",
                        "diary_read",
                    ],
                    "description": (
                        "Operation to perform. "
                        "'wake_up' — load L0 identity + L1 essential story (~600-900 tok). "
                        "'store' — file verbatim content into wing/room. "
                        "'search' — semantic search across palace (optional wing/room filter). "
                        "'recall' — L2 on-demand: all drawers in a wing/room. "
                        "'status' — overview: total drawers, wings, rooms. "
                        "'list_wings' — all wings with counts. "
                        "'list_rooms' — rooms in a wing (or all). "
                        "'taxonomy' — full wing→room→count tree. "
                        "'delete_drawer' — remove drawer by ID. "
                        "'traverse' — walk graph from a room (tunnels). "
                        "'find_tunnels' — rooms bridging wing_a↔wing_b. "
                        "'kg_query' — entity relationships from knowledge graph. "
                        "'kg_add' — add subject→predicate→object fact. "
                        "'kg_invalidate' — mark a fact as ended. "
                        "'kg_timeline' — chronological fact timeline. "
                        "'diary_write' — write agent diary entry. "
                        "'diary_read' — read recent diary entries."
                    ),
                },
                # ── Storage params ──
                "content": {
                    "type": "string",
                    "description": "[store/diary_write] The verbatim text to memorise.",
                },
                "visible": {
                    "type": "boolean",
                    "description": (
                        "[store] If true, appends the raw stored content to the return message "
                        "so user sees exactly what was saved without agent repeating it. "
                        "Default: false (metadata only)."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": (
                        "[store/recall/list_rooms/find_tunnels] Top-level domain, e.g. "
                        "'wing_code', 'wing_user', 'wing_myproject'. "
                        "Use official naming: wing_user, wing_agent, wing_team, wing_code, "
                        "wing_ai_research, wing_hardware, or a custom 'wing_<project>'."
                    ),
                },
                "room": {
                    "type": "string",
                    "description": (
                        "[store/recall/search] Sub-topic slug, e.g. 'chromadb-setup', "
                        "'api-design', 'gpu-pricing'. Use hyphenated slugs."
                    ),
                },
                "hall": {
                    "type": "string",
                    "description": (
                        "[store] Memory type hall: hall_facts, hall_events, "
                        "hall_discoveries, hall_preferences, hall_advice, hall_diary."
                    ),
                },
                "source_file": {
                    "type": "string",
                    "description": "[store] Source file path or URL this memory came from (optional).",
                },
                # ── Search params ──
                "query": {
                    "type": "string",
                    "description": "[search] Semantic search query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "[search/recall] Maximum results to return. Default 5.",
                },
                # ── Drawer management ──
                "drawer_id": {
                    "type": "string",
                    "description": "[delete_drawer] Drawer ID to delete (from store result).",
                },
                # ── Graph traversal ──
                "start_room": {
                    "type": "string",
                    "description": "[traverse] Room slug to start graph walk from.",
                },
                "max_hops": {
                    "type": "integer",
                    "description": "[traverse] Max hops to follow. Default 2.",
                },
                "wing_a": {
                    "type": "string",
                    "description": "[find_tunnels] First wing to bridge from.",
                },
                "wing_b": {
                    "type": "string",
                    "description": "[find_tunnels] Second wing to bridge to.",
                },
                # ── Knowledge graph ──
                "entity": {
                    "type": "string",
                    "description": "[kg_query/kg_timeline] Entity name to query.",
                },
                "subject": {
                    "type": "string",
                    "description": "[kg_add/kg_invalidate] Subject entity.",
                },
                "predicate": {
                    "type": "string",
                    "description": "[kg_add/kg_invalidate] Relationship type.",
                },
                "object": {
                    "type": "string",
                    "description": "[kg_add/kg_invalidate] Object entity.",
                },
                "valid_from": {
                    "type": "string",
                    "description": "[kg_add] When this fact became true (YYYY-MM-DD).",
                },
                "ended": {
                    "type": "string",
                    "description": "[kg_invalidate] When fact stopped being true (YYYY-MM-DD).",
                },
                "as_of": {
                    "type": "string",
                    "description": "[kg_query] Only facts valid at this date (YYYY-MM-DD).",
                },
                "direction": {
                    "type": "string",
                    "description": "[kg_query] outgoing, incoming, or both (default: both).",
                },
                # ── Diary ──
                "agent_name": {
                    "type": "string",
                    "description": "[diary_write/diary_read] Agent name for the diary.",
                },
                "topic": {
                    "type": "string",
                    "description": "[diary_write] Topic/tag for this diary entry.",
                },
                "last_n": {
                    "type": "integer",
                    "description": "[diary_read] Number of recent entries to return. Default 10.",
                },
                # ── Wake-up ──
                "wake_wing": {
                    "type": "string",
                    "description": "[wake_up] Optional wing filter for L1 (project-specific wake-up).",
                },
            },
            "required": ["action"],
        }

    # ── Public entry point ─────────────────────────────────────────────────────

    async def execute(  # noqa: PLR0911, PLR0912, C901
        self,
        action: str,
        # storage
        content: str = "",
        wing: str = "",
        room: str = "",
        hall: str = "",
        source_file: str = "",
        visible: bool = False,
        # search
        query: str = "",
        limit: int = 5,
        # drawer
        drawer_id: str = "",
        # graph
        start_room: str = "",
        max_hops: int = 2,
        wing_a: str = "",
        wing_b: str = "",
        # kg
        entity: str = "",
        subject: str = "",
        predicate: str = "",
        object: str = "",
        valid_from: str = "",
        ended: str = "",
        as_of: str = "",
        direction: str = "both",
        # diary
        agent_name: str = "",
        topic: str = "general",
        last_n: int = 10,
        # wake-up
        wake_wing: str = "",
        **_: Any,
    ) -> str:
        try:
            mp = await asyncio.to_thread(_locked, _import_official)

            # ── Wake-up: L0 + L1 ──────────────────────────────────────────────
            if action == "wake_up":
                stack = await asyncio.to_thread(_locked, mp["MemoryStack"], palace_path=_PALACE_PATH)
                text = await asyncio.to_thread(_locked, stack.wake_up, wing=wake_wing or None)
                return text

            # ── Recall: L2 on-demand ──────────────────────────────────────────
            elif action == "recall":
                stack = await asyncio.to_thread(_locked, mp["MemoryStack"], palace_path=_PALACE_PATH)
                return await asyncio.to_thread(
                    _locked, stack.recall, wing=wing or None, room=room or None, n_results=limit,
                )

            # ── Store: add_drawer ─────────────────────────────────────────────
            elif action == "store":
                if not content:
                    return "❌ Error: 'content' is required for store."
                if not wing:
                    return "❌ Error: 'wing' is required for store (e.g. 'wing_code')."
                if not room:
                    return "❌ Error: 'room' is required for store (e.g. 'api-design')."
                # visible is already a named parameter
                result = await asyncio.to_thread(_locked, mp["add_drawer"],
                    wing=wing,
                    room=room,
                    content=content,
                    source_file=source_file or None,
                    added_by="nanobot",
                )
                if isinstance(result, dict) and result.get("success"):
                    drawer = result.get("drawer_id", "")
                    reason = result.get("reason", "")
                    note = " (already existed, skipped duplicate)" if reason == "already_exists" else ""
                    if not visible:
                        return (
                            f"✅ Stored in {wing}/{room}{note} (hidden)\n"
                            f"  Drawer ID: {drawer}"
                        )
                    return (
                        f"✅ Stored in {wing}/{room}{note}\n"
                        f"  Drawer ID: {drawer}\n"
                        f"  Chars: {len(content)}"
                        + (f"\n  Hall: {hall}" if hall else "")
                        + f"\n\n{content}"
                    )
                return _fmt(result)

            # ── Search ────────────────────────────────────────────────────────
            elif action == "search":
                if not query:
                    return "❌ Error: 'query' is required for search."
                result = await asyncio.to_thread(
                    _locked, mp["search"],
                    query=query,
                    limit=limit,
                    wing=wing or None,
                    room=room or None,
                )
                if isinstance(result, dict):
                    hits = result.get("results", [])
                    if not hits and "error" not in result:
                        return f"🔍 No results for '{query}'."
                    lines = [f"🔍 {len(hits)} result(s) for '{query}':"]
                    for i, h in enumerate(hits, 1):
                        lines.append(
                            f"\n[{i}] {h.get('wing', '?')}/{h.get('room', '?')} "
                            f"(sim={h.get('similarity', '?')})"
                        )
                        preview = str(h.get("text", ""))[:400]
                        if len(str(h.get("text", ""))) > 400:
                            preview += "…"
                        lines.append(f"  {preview}")
                    return "\n".join(lines)
                return _fmt(result)

            # ── Status ────────────────────────────────────────────────────────
            elif action == "status":
                result = await asyncio.to_thread(_locked, mp["status"])
                if isinstance(result, dict):
                    total = result.get("total_drawers", 0)
                    wings = result.get("wings", {})
                    path = result.get("palace_path", _PALACE_PATH)
                    lines = [
                        f"🧠 Memory Palace Status",
                        f"  Path    : {path}",
                        f"  Drawers : {total}",
                        f"  Wings   : {len(wings)}",
                    ]
                    for w, cnt in sorted(wings.items(), key=lambda x: -x[1]):
                        lines.append(f"    ├─ {w}: {cnt} drawers")
                    return "\n".join(lines)
                return _fmt(result)

            # ── List Wings ────────────────────────────────────────────────────
            elif action == "list_wings":
                result = await asyncio.to_thread(_locked, mp["list_wings"])
                if isinstance(result, dict):
                    wings = result.get("wings", {})
                    if not wings:
                        return "Palace is empty — no wings yet."
                    lines = [f"🏛 Wings ({len(wings)}):"]
                    for w, cnt in sorted(wings.items(), key=lambda x: -x[1]):
                        lines.append(f"  ├─ {w}: {cnt} drawers")
                    return "\n".join(lines)
                return _fmt(result)

            # ── List Rooms ────────────────────────────────────────────────────
            elif action == "list_rooms":
                result = await asyncio.to_thread(_locked, mp["list_rooms"], wing=wing or None)
                if isinstance(result, dict):
                    rooms = result.get("rooms", {})
                    w_label = wing or "all wings"
                    if not rooms:
                        return f"No rooms found in {w_label}."
                    lines = [f"🚪 Rooms in {w_label} ({len(rooms)}):"]
                    for r, cnt in sorted(rooms.items(), key=lambda x: -x[1]):
                        lines.append(f"  ├─ {r}: {cnt} drawers")
                    return "\n".join(lines)
                return _fmt(result)

            # ── Taxonomy ──────────────────────────────────────────────────────
            elif action == "taxonomy":
                result = await asyncio.to_thread(_locked, mp["taxonomy"])
                if isinstance(result, dict):
                    tax = result.get("taxonomy", {})
                    if not tax:
                        return "Palace is empty."
                    lines = ["🗺 Palace Taxonomy:"]
                    for w, rooms in sorted(tax.items()):
                        total = sum(rooms.values())
                        lines.append(f"  ├─ {w}  ({total} drawers)")
                        for r, cnt in sorted(rooms.items(), key=lambda x: -x[1]):
                            lines.append(f"  │   └─ {r}: {cnt}")
                    return "\n".join(lines)
                return _fmt(result)

            # ── Delete Drawer ─────────────────────────────────────────────────
            elif action == "delete_drawer":
                if not drawer_id:
                    return "❌ Error: 'drawer_id' is required for delete_drawer."
                result = await asyncio.to_thread(_locked, mp["delete_drawer"], drawer_id=drawer_id)
                if isinstance(result, dict) and result.get("success"):
                    return f"🗑️ Deleted drawer: {drawer_id}"
                return _fmt(result)

            # ── Traverse (graph walk) ─────────────────────────────────────────
            elif action == "traverse":
                if not start_room:
                    return "❌ Error: 'start_room' is required for traverse."
                result = await asyncio.to_thread(_locked, mp["traverse"], start_room=start_room, max_hops=max_hops)
                return _fmt(result)

            # ── Find Tunnels ──────────────────────────────────────────────────
            elif action == "find_tunnels":
                result = await asyncio.to_thread(_locked, mp["find_tunnels"],
                    wing_a=wing_a or None,
                    wing_b=wing_b or None,
                )
                return _fmt(result)

            # ── Knowledge Graph ───────────────────────────────────────────────
            elif action == "kg_query":
                if not entity:
                    return "❌ Error: 'entity' is required for kg_query."
                result = await asyncio.to_thread(_locked, mp["kg_query"], entity=entity, as_of=as_of or None, direction=direction)
                if isinstance(result, dict):
                    facts = result.get("facts", [])
                    lines = [f"🔗 KG: {entity} ({len(facts)} facts)"]
                    for f in facts:
                        lines.append(f"  {f}")
                    return "\n".join(lines)
                return _fmt(result)

            elif action == "kg_add":
                if not (subject and predicate and object):
                    return "❌ Error: 'subject', 'predicate', 'object' are required for kg_add."
                result = await asyncio.to_thread(_locked, mp["kg_add"],
                    subject=subject,
                    predicate=predicate,
                    object=object,
                    valid_from=valid_from or None,
                )
                if isinstance(result, dict) and result.get("success"):
                    return f"✅ KG fact added: {result.get('fact')}"
                return _fmt(result)

            elif action == "kg_invalidate":
                if not (subject and predicate and object):
                    return "❌ Error: 'subject', 'predicate', 'object' are required for kg_invalidate."
                result = await asyncio.to_thread(_locked, mp["kg_invalidate"],
                    subject=subject,
                    predicate=predicate,
                    object=object,
                    ended=ended or None,
                )
                if isinstance(result, dict) and result.get("success"):
                    return f"✅ KG fact invalidated: {result.get('fact')} (ended={result.get('ended')})"
                return _fmt(result)

            elif action == "kg_timeline":
                result = await asyncio.to_thread(_locked, mp["kg_timeline"], entity=entity or None)
                return _fmt(result)

            # ── Diary ─────────────────────────────────────────────────────────
            elif action == "diary_write":
                if not agent_name:
                    return "❌ Error: 'agent_name' is required for diary_write."
                if not content:
                    return "❌ Error: 'content' is required for diary_write."
                result = await asyncio.to_thread(_locked, mp["diary_write"], agent_name=agent_name, entry=content, topic=topic)
                if isinstance(result, dict) and result.get("success"):
                    return (
                        f"📓 Diary entry written for {agent_name}\n"
                        f"  Topic: {topic}\n"
                        f"  ID: {result.get('entry_id', '')}\n"
                        f"  At: {result.get('timestamp', '')}"
                    )
                return _fmt(result)

            elif action == "diary_read":
                if not agent_name:
                    return "❌ Error: 'agent_name' is required for diary_read."
                result = await asyncio.to_thread(_locked, mp["diary_read"], agent_name=agent_name, last_n=last_n)
                if isinstance(result, dict):
                    entries = result.get("entries", [])
                    if not entries:
                        return f"📓 No diary entries for {agent_name}."
                    lines = [
                        f"📓 {agent_name} diary — {len(entries)}/{result.get('total', 0)} entries:"
                    ]
                    for e in entries:
                        lines.append(f"\n[{e.get('date', '?')}] [{e.get('topic', '')}]")
                        lines.append(e.get("content", "")[:500])
                    return "\n".join(lines)
                return _fmt(result)

            else:
                return (
                    f"❌ Unknown action '{action}'. "
                    "Supported: wake_up, store, search, recall, status, list_wings, list_rooms, "
                    "taxonomy, delete_drawer, traverse, find_tunnels, "
                    "kg_query, kg_add, kg_invalidate, kg_timeline, diary_write, diary_read."
                )

        except Exception as exc:
            logger.error("MemoryPalace execute error (action={}): {}", action, exc)
            return f"❌ Error executing memory_palace[{action}]: {exc}"
