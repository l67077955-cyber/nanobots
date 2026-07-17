"""Debug session: provider + CollabBus sandbox + AgentRunner handles."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanobot.debug.stub_provider import StubLLMProvider
from nanobot.groupchat.runtime.agent_runner import AgentRunner
from nanobot.groupchat.runtime.mailbox import MailboxHub
from nanobot.providers.base import LLMProvider


class _OpenTask:
    """Non-done task stand-in so AgentRunner.state stays idle/busy (not done)."""

    def done(self) -> bool:
        return False

    def cancel(self) -> bool:
        return True


@dataclass
class DebugSession:
    """In-process debug context (does not attach to a running gateway)."""

    live: bool
    provider: LLMProvider
    mailbox: MailboxHub
    agents: list[str] = field(default_factory=list)
    runners: dict[str, AgentRunner] = field(default_factory=dict)
    model: str | None = None
    config: Any = None
    notes: list[str] = field(default_factory=list)
    gc_settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        live: bool = False,
        agents: list[str] | None = None,
        model: str | None = None,
        config_path: str | Path | None = None,
    ) -> "DebugSession":
        from nanobot.groupchat.runtime.broadcast_orchestrator import load_groupchat_settings

        agent_names = list(agents or ["A", "B"])
        notes: list[str] = []
        cfg = None
        resolved_model = model
        gc_settings = load_groupchat_settings()

        if live:
            from nanobot.cli.commands import _load_runtime_config, _make_provider

            cfg = _load_runtime_config(
                str(config_path) if config_path else None,
                None,
            )
            provider = _make_provider(cfg)
            resolved_model = model or cfg.agents.defaults.model
            notes.append(f"live provider model={resolved_model}")
            if agents is None:
                agents_dir = Path.home() / ".nanobot" / "agents"
                if agents_dir.is_dir():
                    discovered = sorted(
                        p.name
                        for p in agents_dir.iterdir()
                        if p.is_dir() and not p.name.startswith(".")
                    )
                    if discovered:
                        agent_names = discovered[:4]
                        notes.append(f"agents from disk: {agent_names}")
        else:
            provider = StubLLMProvider(default_model=model or "stub/model")
            notes.append("dry-run stub provider (pass --live for real API)")

        notes.append(
            "timeouts call={call}s leader={leader}s global={glob}s allocate={alloc}s".format(
                call=gc_settings.get("call_timeout"),
                leader=gc_settings.get("leader_call_timeout"),
                glob=gc_settings.get("global_timeout"),
                alloc=gc_settings.get("allocate_timeout"),
            )
        )

        mailbox = MailboxHub()
        for name in agent_names:
            mailbox.create(name)
        mailbox.start_round(agent_names)

        open_task = _OpenTask()
        runners: dict[str, AgentRunner] = {}
        for name in agent_names:
            runners[name] = AgentRunner(
                name, mailbox, lambda t=open_task: t  # type: ignore[arg-type,return-value]
            )

        return cls(
            live=live,
            provider=provider,
            mailbox=mailbox,
            agents=agent_names,
            runners=runners,
            model=resolved_model,
            config=cfg,
            notes=notes,
            gc_settings=gc_settings,
        )

    @property
    def bus(self) -> MailboxHub:
        """CollabBus implementation (MailboxHub satisfies the Protocol)."""
        return self.mailbox

    def runner(self, name: str) -> AgentRunner:
        return self.runners[self._resolve_agent(name)]

    def _resolve_agent(self, name: str) -> str:
        for a in self.agents:
            if a.lower() == name.lower():
                return a
        raise KeyError(f"unknown agent {name!r}; known={self.agents}")


def load_active_agents_from_disk() -> list[str]:
    path = Path.home() / ".nanobot" / "active_agents.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []
