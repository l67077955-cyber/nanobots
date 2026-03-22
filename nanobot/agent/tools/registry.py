"""Tool registry for dynamic tool management."""

from typing import Any

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: str, params: dict[str, Any]) -> Any:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            # Recover malformed params from weak models
            original = dict(params)
            params = self._recover_params(params)
            if params != original:
                from loguru import logger
                logger.debug("Recovered params for {}: {} → {}", name,
                            list(original.keys()), list(params.keys()))

            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)
            
            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                from loguru import logger
                logger.warning("Tool {} param error: {} | raw keys: {}", name, errors, list(original.keys()))
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _HINT

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    @staticmethod
    def _recover_params(params: dict[str, Any]) -> dict[str, Any]:
        """Try to recover malformed parameters from weak models.

        Common issues:
        - Model wraps args in 'raw_arguments': '{"path": "...", "content": "..."}'
        - Model sends stringified JSON as a single value
        """
        import json as _json

        # Case 1: {"raw_arguments": '{"path": "...", "content": "..."}'}
        if "raw_arguments" in params and len(params) == 1:
            raw = params["raw_arguments"]
            if isinstance(raw, str):
                try:
                    parsed = _json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, TypeError):
                    pass
            elif isinstance(raw, dict):
                return raw

        # Case 2: A single key has a JSON string that contains the real params
        if len(params) == 1:
            key, val = next(iter(params.items()))
            if isinstance(val, str) and val.startswith("{"):
                try:
                    parsed = _json.loads(val)
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, TypeError):
                    pass

        return params
