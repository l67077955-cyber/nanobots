"""Tool registry management for GroupChatEngine.

Manages per-agent tool registries with caching, supporting different
workspace scopes for different agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger


class ToolRegistryManager:
    """Manages per-agent tool registries with caching.

    Each agent may have a different workspace scope, requiring different
    tool registry instances. This manager caches registries by workspace path.
    """

    def __init__(
        self,
        default_workspace: Path,
        provider: Any,
        config: Any,
        web_search_config: Any = None,
        web_proxy: str | None = None,
        send_outbound_fn: Any = None,
        mcp_servers: dict | None = None,
    ):
        """Initialize the tool registry manager.

        Args:
            default_workspace: Default workspace path.
            provider: LLM provider instance.
            config: GroupChatConfig instance.
            web_search_config: Web search configuration.
            web_proxy: Web proxy URL.
            send_outbound_fn: Outbound message callback.
            mcp_servers: MCP server configurations.
        """
        self._default_workspace = default_workspace
        self._provider = provider
        self._config = config
        self._web_search_config = web_search_config
        self._web_proxy = web_proxy
        self._send_outbound_fn = send_outbound_fn
        self._mcp_servers = mcp_servers or {}

        # Cache: workspace_path_str -> ToolRegistry
        self._cache: dict[str, Any] = {}

        # Build default registry
        self._default_registry = self._build_registry(default_workspace)

    def _build_registry(self, workspace: Path) -> Any:
        """Build a ToolRegistry scoped to the given workspace.

        Args:
            workspace: Workspace path for tool scope.

        Returns:
            Configured ToolRegistry instance.
        """
        from nanobot.groupchat.orchestra.tools.chatroom_tools import (
            ChatroomSendTool,
            SmartFetchTool,
            SmartSearchTool,
            WaitTool,
        )
        from nanobot.tools.filesystem import (
            EditFileTool,
            ListDirTool,
            ReadFileTool,
            WriteFileTool,
        )
        from nanobot.tools.registry import ToolRegistry
        from nanobot.tools.shell import ExecTool
        from nanobot.tools.web import WebFetchTool, WebSearchTool

        registry = ToolRegistry()

        # Web tools
        raw_search = WebSearchTool(config=self._web_search_config, proxy=self._web_proxy)
        registry.register(SmartSearchTool(raw_search, provider=self._provider))
        raw_fetch = WebFetchTool()
        registry.register(SmartFetchTool(raw_fetch, provider=self._provider))

        # Shell tool
        registry.register(ExecTool(timeout=120, working_dir=str(workspace)))

        # Process tool
        from nanobot.tools.process import ProcessTool
        registry.register(ProcessTool())

        # Filesystem tools
        registry.register(ReadFileTool(workspace=workspace, allowed_dir=None))
        registry.register(WriteFileTool(workspace=workspace, allowed_dir=None))
        registry.register(EditFileTool(workspace=workspace, allowed_dir=None))
        registry.register(ListDirTool(workspace=workspace, allowed_dir=None))

        # Message tool (if outbound callback configured)
        if self._send_outbound_fn:
            from nanobot.tools.message import MessageTool
            registry.register(MessageTool(send_callback=self._send_outbound_fn))

        return registry

    def get_registry(
        self,
        agent_name: str,
        workspace_scope: str,
        agent_cfg: dict[str, Any] | None = None,
    ) -> Any:
        """Get or create a tool registry for an agent's workspace scope.

        Args:
            agent_name: Agent name for logging.
            workspace_scope: Workspace scope ("workspace", "source", "prompts", "tmp", or path).
            agent_cfg: Optional agent config dict.

        Returns:
            ToolRegistry instance for the agent's workspace.
        """
        workspace = self._resolve_workspace(workspace_scope, agent_cfg)
        key = str(workspace)

        if key not in self._cache:
            registry = self._build_registry(workspace)
            # Add chatroom tools
            from nanobot.groupchat.orchestra.tools.chatroom_tools import ChatroomSendTool, WaitTool
            # Note: MailboxHub must be injected separately
            self._cache[key] = registry
            logger.info("ToolRegistryManager: built registry for {} -> {}", agent_name, workspace)

        return self._cache[key]

    def _resolve_workspace(self, scope: str, agent_cfg: dict[str, Any] | None) -> Path:
        """Resolve workspace path from scope and agent config.

        Args:
            scope: Workspace scope identifier.
            agent_cfg: Agent configuration dict.

        Returns:
            Resolved workspace path.
        """
        if scope == "workspace":
            return self._default_workspace
        elif scope == "source":
            import nanobot
            return Path(nanobot.__file__).parent.parent
        elif scope == "prompts":
            if agent_cfg and agent_cfg.get("agent_dir"):
                return Path(agent_cfg["agent_dir"])
            return self._default_workspace
        elif scope == "tmp":
            return Path("/tmp")
        elif scope.startswith("/"):
            return Path(scope)
        else:
            logger.warning("Unknown workspace scope '{}', using default", scope)
            return self._default_workspace

    def add_chatroom_tools(self, registry: Any, mailbox: Any) -> None:
        """Add chatroom communication tools to a registry.

        Args:
            registry: ToolRegistry instance.
            mailbox: MailboxHub instance.
        """
        from nanobot.groupchat.orchestra.tools.chatroom_tools import ChatroomSendTool, WaitTool
        registry.register(ChatroomSendTool(mailbox=mailbox))
        registry.register(WaitTool(mailbox=mailbox))

    def clear_cache(self, agent_name: str | None = None) -> None:
        """Clear registry cache.

        Args:
            agent_name: Optional agent name. If provided, only clear that agent's cache.
        """
        if agent_name is None:
            self._cache.clear()
        else:
            # Find and remove entries for this agent's workspace
            keys_to_remove = [k for k in self._cache if agent_name.lower() in k.lower()]
            for key in keys_to_remove:
                self._cache.pop(key, None)

    @property
    def default_registry(self) -> Any:
        """Get the default tool registry."""
        return self._default_registry
