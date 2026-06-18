import json as _json
import time as _t
from typing import Any

from loguru import logger

from nanobot.groupchat.display import display as _d
from nanobot.groupchat.orchestra.events import trigger_realtime_interrupts


class BroadcastView:
    """Handles Telegram UI rendering for broadcast events."""

    def __init__(
        self,
        engine: Any,
        tracker: Any,
        mailbox: Any,
        pool: Any,
        search_pool: Any,
        agents: list[str],
        leader_name: str | None,
        agent_ranks: dict[str, int] | None = None,
    ):
        self.engine = engine
        self.tracker = tracker
        self.mailbox = mailbox
        self.pool = pool
        self.search_pool = search_pool
        self.agents = agents
        self.leader_name = leader_name
        self.agent_ranks = agent_ranks or {}

        self.pending_tool_msgs: dict[str, tuple[int | None, str]] = {}
        self.last_chatroom_send_to: dict[str, list[str]] = {a: [] for a in agents}

    async def on_tool_start(
        self,
        name: str,
        tool_name: str,
        args: dict,
        tool_call_id: str,
        cycle_t0: float,
        cycle_usage: dict,
    ) -> None:
        """Render tool start event."""
        await self.tracker.update_from_tool_start(name, tool_name, args)

        self.engine._save_event("tool_call", agent=name, extra={
            "tool": tool_name,
            "args": {k: (v if isinstance(v, str) else v) for k, v in args.items()},
        })

        logger.info(
            "broadcast [{}] tool_call: {}({})",
            name, tool_name, _json.dumps(args, ensure_ascii=False)[:300],
        )

        if tool_name == "chatroom_send":
            raw_to = args.get("to", "?")
            if isinstance(raw_to, list):
                to_list = [str(t).strip() for t in raw_to if t]
            elif isinstance(raw_to, str):
                to_list = [s for s in [raw_to.strip()] if s]
            else:
                to_list = []
            if not to_list:
                to_list = ["?"]

            self.last_chatroom_send_to[name] = to_list
            msg_full = (args.get("message", "") or "")
            to_str = ", ".join(to_list)

            elapsed = _t.time() - cycle_t0
            tok_t = cycle_usage.get("total_tokens", 0)
            stats_suffix = ""
            if tok_t > 0:
                p = cycle_usage.get("prompt_tokens", 0)
                c = cycle_usage.get("completion_tokens", 0)
                stats_suffix = "\n" + _d.format_token_stats(p, c, elapsed=elapsed)

            await self.engine._send(
                _d.chatroom_send_msg(name, to_str, msg_full + stats_suffix, leader=self.leader_name)
            )

        elif tool_name == "wait":
            pass

        else:
            line = _d.tool_activity_msg(
                name, tool_name, args, leader=self.leader_name, agent_ranks=self.agent_ranks,
            )
            text = f"🟡 {line}"
            msg_id = None
            if self.engine._send_and_get_id_fn:
                try:
                    msg_id = await self.engine._send_and_get_id_fn(text)
                except Exception:
                    await self.engine._send(text)
            else:
                await self.engine._send(text)
            self.pending_tool_msgs[tool_call_id] = (msg_id, text)

    async def on_tool_result(
        self,
        name: str,
        tool_name: str,
        tool_call_id: str,
        result: str,
    ) -> None:
        """Render tool result event."""
        result_str = str(result or "")
        await self.tracker.update_from_tool_result(name, tool_name, result_str)

        self.engine._save_event("tool_result", agent=name, extra={
            "tool": tool_name,
            "result_len": len(result_str),
            "success": not result_str.startswith("Error:"),
        })

        logger.info(
            "broadcast [{}] tool_result: {} ({}c): {}",
            name, tool_name, len(result_str), result_str,
        )

        if tool_name == "chatroom_send" and result_str:
            if "BLOCKED:" in result_str:
                await self.engine._send(
                    f"✗ {name} dropped ── {self.pool.status()}"
                )
            elif "threads]" in result_str:
                await self.engine._send(
                    f"  {self.pool.status()}"
                )
                await trigger_realtime_interrupts(
                    sender=name,
                    targets=self.last_chatroom_send_to.get(name, []),
                    mailbox=self.mailbox,
                    engine=self.engine,
                    leader_name=self.leader_name,
                )
            else:
                await trigger_realtime_interrupts(
                    sender=name,
                    targets=self.last_chatroom_send_to.get(name, []),
                    mailbox=self.mailbox,
                    engine=self.engine,
                    leader_name=self.leader_name,
                )

        elif tool_name == "wait" and result_str and not result_str.startswith("⏰"):
            await self.engine._send(_d.chatroom_wait_msg(name, result_str, leader=self.leader_name))

        elif tool_name not in ("chatroom_send", "wait") and result_str:
            brief = _d.tool_result_brief(name, tool_name, result_str)
            if tool_name == "web_search" and self.search_pool:
                self.search_pool.on_output(name)
                brief += f"  🔧 {self.search_pool.status()}"
            elif self.search_pool:
                self.search_pool.on_output(name)

            pending = self.pending_tool_msgs.pop(tool_call_id, None)
            if pending:
                msg_id, original_text = pending
                success = not result_str.startswith("Error:")
                icon = "🟢" if success else "🔴"
                updated = original_text.replace("🟡", icon) + f"\n{brief}"
                if msg_id and self.engine._edit_fn:
                    try:
                        await self.engine._edit_fn(msg_id, updated)
                    except Exception:
                        await self.engine._send(updated)
                else:
                    await self.engine._send(updated)
            else:
                await self.engine._send(brief)