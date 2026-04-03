"""state_bus.py — state.yaml 纯变量驱动的状态总线（FileStateBus）。

Leader 通过 edit_file 直接修改 state.yaml 的变量来控制一切。
系统通过 poll_changes() 检测变量变化并执行对应操作。

╔══════════════════════════════════════════════════════════════╗
║  state.yaml 结构:                                            ║
║                                                              ║
║  session:        ← 会话元数据（leader、话题等）              ║
║  agents:         ← 每个 agent 的控制变量 + 状态变量          ║
║  conversation:   ← 对话历史记录                              ║
║  leader_data:    ← leader 的自由存储区                       ║
╚══════════════════════════════════════════════════════════════╝

控制方式：
    Leader 新增 agent block     → 系统启动 agent
    Leader 删除 agent block     → 系统取消并移除 agent
    Leader 改 state: paused     → 系统暂停 agent
    Leader 改 muted: true       → 系统禁言 agent
    Leader 改 context_exclude   → 系统更新上下文过滤
    Leader 改 reply_to          → 系统更新回复目标

写入方式：原子写入（写临时文件 → os.replace），避免竞态。

⚠️ agent 修改本文件时注意：
    1. _read_all() / _write_all() 是底层 I/O，加了线程锁 _lock
    2. 所有公共方法都是 read → modify → write 三步，不可拆分
    3. state.yaml 的 key 名字不可改（leader prompt 中有文档）
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from loguru import logger

# ── YAML / JSON backend ──────────────────────────────────────────

try:
    import yaml as _yaml

    def _dump(data: Any, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                _yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(path: Path) -> Any:
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return _yaml.safe_load(f)
        except Exception:
            return None

except ImportError:
    import json as _json

    def _dump(data: Any, path: Path) -> None:  # type: ignore[misc]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                f.write("\n")
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load(path: Path) -> Any:  # type: ignore[misc]
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return _json.load(f)


# ── FileStateBus ──────────────────────────────────────────────────


class FileStateBus:
    """Single-file state bus for a group chat session.

    All state lives in ``<session_dir>/state.yaml``.
    Thread-safe via a single reentrant lock.

    Leader 直接修改变量，系统通过 poll_changes() 检测变化。
    """

    def __init__(self, session_dir: Path) -> None:
        self._root = session_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._file = self._root / "state.yaml"
        self._lock = threading.RLock()
        self._seq = self._recover_seq()
        # 快照 — poll_changes() 用于 diff
        self._prev_snapshot: dict = {}
        logger.info("FileStateBus: initialized at {} (seq={})", self._file, self._seq)

    # ── Low-level I/O ─────────────────────────────────────────

    def _read_all(self) -> dict:
        with self._lock:
            data = _load(self._file)
            data_dict = data if isinstance(data, dict) else {}
            try:
                from nanobot.groupchat.state_models import GroupChatStateData
                model = GroupChatStateData.model_validate(data_dict)
                return model.model_dump(by_alias=True)
            except Exception as e:
                logger.warning("FileStateBus: State YAML validation error on read: {}", e)
                return data_dict

    def _write_all(self, data: dict) -> None:
        with self._lock:
            try:
                from nanobot.groupchat.state_models import GroupChatStateData
                model = GroupChatStateData.model_validate(data)
                validated_data = model.model_dump(by_alias=True)
                _dump(validated_data, self._file)
            except Exception as e:
                logger.warning("FileStateBus: State YAML validation error on write: {}", e)
                _dump(data, self._file)

    def _update(self, mutator) -> None:
        """Read → mutate → write cycle under lock."""
        with self._lock:
            data = self._read_all()
            mutator(data)
            self._write_all(data)

    # ── Timestamp helper ──────────────────────────────────────

    @staticmethod
    def _now() -> str:
        from nanobot.groupchat.utils import cn_now
        return cn_now().isoformat()

    def _recover_seq(self) -> int:
        """Recover _seq from existing conversation to avoid duplicate seq after restart."""
        try:
            data = _load(self._file)
            if isinstance(data, dict):
                conv = data.get("conversation", [])
                if conv:
                    return max((m.get("seq", 0) for m in conv if isinstance(m, dict)), default=0)
        except Exception:
            pass
        return 0

    # ── Session ───────────────────────────────────────────────

    def init_session(
        self,
        *,
        leader: str | None,
        topic: str,
        round_num: int,
        active_agents: list[str],
    ) -> None:
        """Create or update ``state.yaml`` — preserves conversation and leader_data across rounds."""
        existing = self._read_all()
        is_fresh = not existing or not existing.get("session")

        data: dict[str, Any] = {
            "session": {
                "id": self._root.name,
                "leader": leader,
                "topic": topic,
                "round": round_num,
            },
            "agents": {},
            # Preserve conversation across rounds
            "conversation": existing.get("conversation", []) if not is_fresh else [],
            # Preserve leader_data across rounds
            "leader_data": existing.get("leader_data", {}) if not is_fresh else {},
        }

        # Initialize per-agent blocks
        existing_agents = existing.get("agents", {}) if not is_fresh else {}
        for name in active_agents:
            if name in existing_agents:
                # Preserve runtime data from previous round
                preserved = existing_agents[name]
                data["agents"][name] = {
                    **self._empty_agent(),
                    "toolchain": preserved.get("toolchain", []),
                    "inbox": preserved.get("inbox", []),
                    "outbox": preserved.get("outbox", []),
                }
            else:
                data["agents"][name] = self._empty_agent()

        self._write_all(data)
        # Take initial snapshot for poll_changes()
        self._prev_snapshot = self._read_all()
        logger.info("FileStateBus: session initialized with {} agents (fresh={})", len(active_agents), is_fresh)

    @staticmethod
    def _empty_agent() -> dict:
        return {
            # Control variables
            "state": "running",
            "reply_to": "All",
            "context_exclude": [],
            "muted": False,
            # Status variables
            "activity": "idle",
            "current_tool": None,
            "cycle": 0,
            "content_preview": "",
            "toolchain": [],
            "inbox": [],
            "outbox": [],
        }

    def update_session(self, **fields: Any) -> None:
        """Update session-level fields."""
        def _mut(data: dict) -> None:
            session = data.setdefault("session", {})
            session.update(fields)
        self._update(_mut)

    # ── Agent State (system updates) ─────────────────────────

    def set_agent_activity(self, name: str, activity: str, **extra: Any) -> None:
        """Update an agent's activity and optional extra status fields.

        ``activity`` is one of: idle, thinking, tool_calling, replying.
        Only status variables are allowed in extra.
        """
        _allowed = {"cycle", "current_tool", "content_preview"}
        filtered = {k: v for k, v in extra.items() if k in _allowed}
        def _mut(data: dict) -> None:
            agents = data.setdefault("agents", {})
            if name not in agents:
                return  # Agent block deleted by leader → don't recreate
            agent = agents[name]
            agent["activity"] = activity
            agent.update(filtered)
        self._update(_mut)

    def update_agent(self, name: str, **fields: Any) -> None:
        """Partially update agent status fields (content_preview, cycle, etc.)."""
        def _mut(data: dict) -> None:
            agents = data.get("agents", {})
            if name not in agents:
                return  # Agent block deleted by leader → don't recreate
            agents[name].update(fields)
        self._update(_mut)

    # ── Toolchain ─────────────────────────────────────────────

    def append_tool_start(self, name: str, tool: str, args: dict) -> None:
        """Record tool call start → sets activity to ``tool_calling``."""
        safe_args = {
            k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
            for k, v in args.items()
        }
        entry = {
            "tool": tool,
            "args": safe_args,
            "started": self._now(),
        }
        def _mut(data: dict) -> None:
            agents = data.get("agents", {})
            if name not in agents:
                return
            agent = agents[name]
            agent["toolchain"].append(entry)
            agent["activity"] = "tool_calling"
            agent["current_tool"] = tool
        self._update(_mut)

    def complete_tool(self, name: str, tool: str, result_len: int, success: bool, preview: str = "") -> None:
        """Record tool result → sets activity back to ``thinking``."""
        def _mut(data: dict) -> None:
            agents = data.get("agents", {})
            if name not in agents:
                return
            agent = agents[name]
            chain = agent.get("toolchain", [])
            if chain:
                last = chain[-1]
                last["finished"] = self._now()
                last["ok"] = success
                last["len"] = result_len
                if preview:
                    last["preview"] = preview[:200]
            agent["activity"] = "thinking"
            agent["current_tool"] = None
        self._update(_mut)

    # ── Inbox / Outbox ────────────────────────────────────────

    def deliver_message(self, sender: str, targets: list[str], content: str, all_agents: list[str] | None = None) -> None:
        """Deliver a message: appends to sender's outbox and each target's inbox."""
        ts = self._now()
        truncated = content[:500]

        # Expand "All"
        actual_targets = list(targets)
        if "All" in targets or "all" in targets:
            if all_agents:
                actual_targets = [a for a in all_agents if a != sender]
            else:
                actual_targets = targets

        def _mut(data: dict) -> None:
            agents = data.setdefault("agents", {})
            # Sender outbox
            if sender in agents:
                agents[sender].setdefault("outbox", []).append({
                    "to": targets,
                    "content": truncated,
                    "ts": ts,
                })

            # Targets inbox
            for tgt in actual_targets:
                if tgt in ("All", "all"):
                    continue
                if tgt in agents:
                    agents[tgt].setdefault("inbox", []).append({
                        "from": sender,
                        "content": truncated,
                        "ts": ts,
                    })
        self._update(_mut)

    # ── Conversation ──────────────────────────────────────────

    def append_conversation(self, sender: str, content: str) -> None:
        """Append a message to the global conversation chain."""
        self._seq += 1
        entry: dict[str, Any] = {
            "seq": self._seq,
            "sender": sender,
            "content": content[:1000],
            "ts": self._now(),
        }
        def _mut(data: dict) -> None:
            data.setdefault("conversation", []).append(entry)
        self._update(_mut)

    def rewrite_conversation(self, messages: list[dict[str, Any]]) -> None:
        """Overwrite the entire global conversation chain."""
        def _mut(data: dict) -> None:
            new_conv = []
            for i, msg in enumerate(messages):
                entry: dict[str, Any] = {
                    "seq": i + 1,
                    "sender": msg.get("sender", "系统"),
                    "content": msg.get("content", "")[:2000],
                    "ts": msg.get("ts", self._now()),
                }
                new_conv.append(entry)
            data["conversation"] = new_conv
            self._seq = len(new_conv)
        self._update(_mut)

    # ── Poll Changes (核心 diff 机制) ─────────────────────────

    def poll_changes(self) -> list[dict[str, Any]]:
        """对比上一次快照和当前 state.yaml，返回变化列表。

        系统每 2 秒调用一次。检测 leader 对变量的修改并返回变化类型。

        Returns:
            List of change dicts, each with a "type" key:
                agent_added    — leader 新增了一个 agent block
                agent_removed  — leader 删掉了一个 agent block
                state_changed  — agent.state 从 running → paused 或反之
                muted_changed  — agent.muted 变化
                conversation_rewritten — leader 重写了 conversation
                session_ended  — leader 设置 session.status: done
        """
        current = self._read_all()
        prev_agents = set(self._prev_snapshot.get("agents", {}).keys())
        curr_agents = set(current.get("agents", {}).keys())

        changes: list[dict[str, Any]] = []

        # Session status 变化
        prev_status = self._prev_snapshot.get("session", {}).get("status", "running")
        curr_status = current.get("session", {}).get("status", "running")
        if prev_status != curr_status and curr_status == "done":
            changes.append({"type": "session_ended"})

        # 新增 agent
        for name in curr_agents - prev_agents:
            agent = current["agents"][name]
            changes.append({
                "type": "agent_added",
                "name": name,
                "state": agent.get("state", "running"),
            })

        # 删除 agent (leader 删了整个 block)
        for name in prev_agents - curr_agents:
            changes.append({"type": "agent_removed", "name": name})

        # 已有 agent 的变量变化
        for name in curr_agents & prev_agents:
            prev = self._prev_snapshot.get("agents", {}).get(name, {})
            curr = current.get("agents", {}).get(name, {})

            if prev.get("state") != curr.get("state"):
                changes.append({
                    "type": "state_changed",
                    "name": name,
                    "old": prev.get("state"),
                    "new": curr.get("state"),
                })

            if prev.get("muted") != curr.get("muted"):
                changes.append({
                    "type": "muted_changed",
                    "name": name,
                    "muted": curr.get("muted"),
                })

        # Conversation 变化（leader 重写了 conversation）
        prev_conv = self._prev_snapshot.get("conversation", [])
        curr_conv = current.get("conversation", [])
        if len(curr_conv) < len(prev_conv):
            # Conversation was rewritten (shortened or replaced)
            changes.append({"type": "conversation_rewritten"})

        # Update snapshot
        self._prev_snapshot = current
        return changes

    def get_agent_control(self, name: str) -> dict[str, Any]:
        """Read an agent's control variables (state, reply_to, context_exclude, muted)."""
        data = self._read_all()
        agent = data.get("agents", {}).get(name, {})
        return {
            "state": agent.get("state", "running"),
            "reply_to": agent.get("reply_to", "All"),
            "context_exclude": agent.get("context_exclude", []),
            "muted": agent.get("muted", False),
        }

    # ── Read helpers ──────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return the full state as a dict."""
        return self._read_all()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._file
