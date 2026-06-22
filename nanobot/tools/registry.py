"""Tool registry for dynamic tool management."""

from typing import Any

from nanobot.tools.base import Tool


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

    async def execute(self, name: str, params: dict[str, Any] | list[Any]) -> Any:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            # Recover malformed params from weak models
            original = params
            params = self._recover_params(params, tool_name=name)
            if params != original:
                from loguru import logger
                logger.debug(
                    "Recovered params for {}: {!r} → {}",
                    name, original, list(params.keys()) if isinstance(params, dict) else params,
                )

            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)
            
            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                from loguru import logger
                raw_keys = list(original.keys()) if isinstance(original, dict) else type(original).__name__
                logger.warning("Tool {} param error: {} | raw: {}", name, errors, raw_keys)
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
    def _recover_list_params(items: list[Any], *, tool_name: str = "") -> dict[str, Any]:
        """Recover tool params when the model emitted a JSON array instead of an object.

        Typical failure: a long heredoc/HTML in exec() breaks JSON parsing; json_repair
        turns the tail into pseudo-objects like {"margin": 0, "padding": 0}.
        """
        commands: list[str] = []
        path = None
        content = None
        long_strings: list[str] = []
        for item in items:
            if isinstance(item, dict):
                if isinstance(item.get("command"), str):
                    commands.append(item["command"])
                if tool_name == "write_file":
                    if path is None and isinstance(item.get("path"), str):
                        path = item["path"]
                    c = item.get("content")
                    if content is None and isinstance(c, str):
                        content = c
                    # harvest long strings anywhere in the dict as potential content
                    for vv in item.values():
                        if isinstance(vv, str) and len(vv) > 100:
                            long_strings.append(vv)
                    if isinstance(item.get("path"), str) and "content" in item:
                        return {"path": item["path"], "content": item.get("content") or ""}
                elif "path" in item and "content" in item:
                    return {"path": item["path"], "content": item["content"]}
            elif isinstance(item, str) and item.strip():
                commands.append(item)
                if len(item) > 80:
                    long_strings.append(item)

        if tool_name == "write_file" and path is not None:
            if content is None and long_strings:
                content = max(long_strings, key=len)
            return {"path": path, "content": content if content is not None else ""}

        if commands:
            if len(commands) == 1:
                return {"command": commands[0]}
            return {"commands": commands}

        return {}

    @staticmethod
    def _recover_params(
        params: dict[str, Any] | list[Any],
        *,
        tool_name: str = "",
    ) -> dict[str, Any]:
        """Try to recover malformed parameters from weak models.

        Common issues:
        - Model wraps args in 'raw_arguments': '{"path": "...", "content": "..."}'
        - Model sends stringified JSON as a single value
        - Model emits a JSON array when large HTML/shell payloads fracture mid-parse
        """
        import json as _json

        if isinstance(params, list):
            return ToolRegistry._recover_list_params(params, tool_name=tool_name)

        if not isinstance(params, dict):
            return {}

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

        # Case 3: write_file with missing/renamed keys from weak models (e.g. grok fast, fractured payload)
        if tool_name == "write_file" and isinstance(params, dict):
            has_path = "path" in params
            has_content = "content" in params
            if has_path and not has_content:
                c = params.get("content") or params.get("text") or params.get("data") or params.get("body") or ""
                p = dict(params)
                p["content"] = c
                return p
            if has_content and not has_path:
                pth = params.get("path") or params.get("file") or params.get("filename") or ""
                p = dict(params)
                p["path"] = pth
                return p

        # Case 4: aggressive salvage for write_file (fractured JSON from long HTML content, glm etc.)
        # Walk structure, extract plausible path + largest string chunk as content.
        if tool_name == "write_file" and isinstance(params, dict):
            salvaged = ToolRegistry._salvage_write_file(params)
            if salvaged:
                return salvaged

        return params

    @staticmethod
    def _salvage_write_file(params: dict[str, Any]) -> dict[str, Any] | None:
        """Deep salvage for write_file when LLM (e.g. glm) mangles large content args.

        Finds any plausible 'path' and the longest string value as 'content'.
        Used when json_repair or argument accumulation produced weird shapes.
        """
        if not isinstance(params, dict) or ("path" in params and "content" in params):
            return None

        import re

        path_candidates: list[str] = []
        content_candidates: list[str] = []

        def walk(o: Any, depth: int = 0) -> None:
            if depth > 5:
                return
            if isinstance(o, dict):
                for k, v in list(o.items()):
                    lk = str(k).lower().strip()
                    if isinstance(v, str):
                        if lk in ("path", "file", "filename", "target", "dest", "filepath"):
                            path_candidates.append(v)
                        if lk in ("content", "text", "data", "body", "html", "code", "value", "payload", "source"):
                            if len(v) > 20:
                                content_candidates.append(v)
                        # always harvest long strings as content (main rescue for huge HTML)
                        if len(v) > 120:
                            content_candidates.append(v)
                        # only short, path-like things go to paths
                        if len(v) < 250 and (v.strip().startswith(("/", "./")) or "/" in v or re.search(r'\.(html?|md|txt|css|js|py|json|tsx?)$', v, re.I)):
                            path_candidates.append(v)
                    walk(v, depth + 1)  # always descend
            elif isinstance(o, list):
                for item in o:
                    walk(item, depth + 1)
            elif isinstance(o, str):
                s = o.strip()
                if len(s) < 250 and (s.startswith(("/", "./")) or "/" in s or re.search(r'\.(html?|md|txt|css|js|py|json|tsx?)$', s, re.I)):
                    path_candidates.append(s)
                elif len(s) > 80:
                    content_candidates.append(s)

        walk(params)

        # Choose best path (prefer ones that look like real file paths)
        path: str | None = None
        for c in path_candidates:
            if isinstance(c, str) and c.strip() and (c.strip().startswith(("/", "./", "~")) or any(c.strip().endswith(ext) for ext in (".html", ".htm", ".md", ".txt"))):
                path = c.strip()
                break
        if not path and path_candidates:
            path = str(path_candidates[0]).strip() or None

        # Choose largest content
        content = ""
        if content_candidates:
            content = max((c for c in content_candidates if isinstance(c, str)), key=len, default="")

        if path and content:
            return {"path": path, "content": content}

        return None
