"""Behavioral tests for the MCP client — nanobot/tools/mcp.py.

Written ahead of the 2026-07-28 MCP spec migration (see
``docs/plan-2026-09-08-industry-followup.md`` batch A).

Companion to ``tests/test_mcp_tool.py``, which already covers ``enabledTools``
filtering and ``MCPToolWrapper.execute`` error handling (timeout, server-side
cancellation, external cancellation, generic exceptions). Those are not
repeated here. What this file adds is the part the migration will move:
transport *selection*, the session handshake, tool registration and naming,
failure isolation between servers, and schema normalization.

What is pinned is *nanobot's* behavior, not the SDK's — the transports
themselves are faked, so these tests must keep passing across SDK versions.

The one place the SDK version leaks in is the arity of the tuple a transport
yields — see ``_STREAMABLE_HTTP_YIELDS`` below.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import pytest

from nanobot.config.schema import MCPServerConfig
from nanobot.tools.mcp import (
    MCPToolWrapper,
    _normalize_schema_for_openai,
    connect_mcp_servers,
)
from nanobot.tools.registry import ToolRegistry

# mcp SDK <2.0 yields (read, write, get_session_id) from streamable_http_client;
# the 2.x rewrite yields (read, write). When the SDK pin moves to 2.x this
# constant flips to 2 and nanobot/tools/mcp.py must be unpacked accordingly.
_STREAMABLE_HTTP_YIELDS = 3


# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeAsyncCM:
    """Minimal async context manager wrapping a fixed value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@dataclass
class _FakeToolDef:
    name: str
    description: str | None = "a tool"
    inputSchema: dict[str, Any] | None = None  # noqa: N815 — mirrors MCP wire name


@dataclass
class _FakeListToolsResult:
    tools: list[_FakeToolDef]


class _FakeSession:
    """Stands in for mcp.ClientSession."""

    def __init__(self, tools: list[_FakeToolDef], call_result: Any = None) -> None:
        self._tools = tools
        self._call_result = call_result
        self.initialized = False
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self) -> _FakeListToolsResult:
        return _FakeListToolsResult(self._tools)

    async def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        self.calls.append((name, arguments or {}))
        return self._call_result


@dataclass
class _TransportSpy:
    """Records which transport was chosen and with what arguments."""

    stdio: list[Any] = field(default_factory=list)
    sse: list[Any] = field(default_factory=list)
    streamable: list[Any] = field(default_factory=list)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tools: list[_FakeToolDef],
    *,
    stdio_error: Exception | None = None,
) -> tuple[_TransportSpy, _FakeSession]:
    """Patch the three transports and ClientSession; return the spy + session."""
    import mcp
    import mcp.client.sse
    import mcp.client.stdio
    import mcp.client.streamable_http

    spy = _TransportSpy()
    session = _FakeSession(tools)

    def _stdio(params: Any, *a: Any, **kw: Any) -> Any:
        spy.stdio.append(params)
        if stdio_error is not None:
            raise stdio_error
        return _FakeAsyncCM(("read", "write"))

    def _sse(url: str, *a: Any, **kw: Any) -> Any:
        spy.sse.append(url)
        return _FakeAsyncCM(("read", "write"))

    def _streamable(url: str, *a: Any, **kw: Any) -> Any:
        spy.streamable.append(url)
        return _FakeAsyncCM(tuple(["read", "write"] + [None] * (_STREAMABLE_HTTP_YIELDS - 2)))

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", _stdio)
    monkeypatch.setattr(mcp.client.sse, "sse_client", _sse)
    monkeypatch.setattr(mcp.client.streamable_http, "streamable_http_client", _streamable)
    monkeypatch.setattr(mcp, "ClientSession", lambda *a, **kw: _FakeAsyncCM(session))
    return spy, session


async def _connect(servers: dict[str, MCPServerConfig]) -> ToolRegistry:
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        await connect_mcp_servers(servers, registry, stack)
    return registry


# ── Transport selection ──────────────────────────────────────────────────


async def test_command_selects_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    await _connect({"srv": MCPServerConfig(command="node", args=["x.js"])})
    assert len(spy.stdio) == 1
    assert spy.stdio[0].command == "node"
    assert spy.stdio[0].args == ["x.js"]
    assert not spy.sse and not spy.streamable


async def test_sse_suffix_url_selects_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    await _connect({"srv": MCPServerConfig(url="https://example.test/sse")})
    assert spy.sse == ["https://example.test/sse"]
    assert not spy.streamable


async def test_plain_url_selects_streamable_http(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    await _connect({"srv": MCPServerConfig(url="https://example.test/mcp")})
    assert spy.streamable == ["https://example.test/mcp"]
    assert not spy.sse


async def test_explicit_type_wins_over_url_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    await _connect(
        {"srv": MCPServerConfig(type="streamableHttp", url="https://example.test/sse")}
    )
    assert spy.streamable == ["https://example.test/sse"]
    assert not spy.sse


async def test_server_without_command_or_url_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy, _ = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    registry = await _connect({"srv": MCPServerConfig()})
    assert len(registry) == 0
    assert not spy.stdio and not spy.sse and not spy.streamable


async def test_unknown_transport_type_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    spy, _ = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    cfg = MCPServerConfig(url="https://example.test/mcp")
    object.__setattr__(cfg, "type", "carrier-pigeon")
    registry = await _connect({"srv": cfg})
    assert len(registry) == 0
    assert not spy.streamable


# ── Session lifecycle ────────────────────────────────────────────────────


async def test_session_is_initialized_before_listing_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2025-11-25 handshake. When the SDK moves to the stateless
    2026-07-28 core this assertion is the one that must be revisited."""
    _, session = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    await _connect({"srv": MCPServerConfig(command="node")})
    assert session.initialized is True


# ── Registration & naming ────────────────────────────────────────────────


async def test_tools_registered_under_wrapped_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, [_FakeToolDef("read_file"), _FakeToolDef("write_file")])
    registry = await _connect({"fs": MCPServerConfig(command="node")})
    assert sorted(registry.tool_names) == ["mcp_fs_read_file", "mcp_fs_write_file"]


async def test_multiple_servers_register_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    registry = await _connect(
        {
            "a": MCPServerConfig(command="node"),
            "b": MCPServerConfig(url="https://example.test/mcp"),
        }
    )
    assert sorted(registry.tool_names) == ["mcp_a_ping", "mcp_b_ping"]


# ── enabledTools filtering ──────────────────────────────────────────────
#
# Covered by tests/test_mcp_tool.py (raw names, wrapped names, "*" default,
# empty list, and the warning on unmatched entries). Not duplicated here.


# ── Failure isolation ────────────────────────────────────────────────────


async def test_failing_server_does_not_block_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead server (the CONNECTION_CLOSED case) must not take down the rest."""
    _install_fakes(
        monkeypatch,
        [_FakeToolDef("ping")],
        stdio_error=RuntimeError("connection closed"),
    )
    registry = await _connect(
        {
            "dead": MCPServerConfig(command="node"),
            "alive": MCPServerConfig(url="https://example.test/mcp"),
        }
    )
    assert registry.tool_names == ["mcp_alive_ping"]


# ── MCPToolWrapper ───────────────────────────────────────────────────────


def _wrapper(tool_def: _FakeToolDef, session: Any, timeout: int = 30) -> MCPToolWrapper:
    return MCPToolWrapper(session, "srv", tool_def, tool_timeout=timeout)


async def test_wrapper_passes_arguments_through_under_the_original_name() -> None:
    """The wrapper is exposed as mcp_<server>_<tool> but must call the server
    with the tool's original name and the caller's kwargs untouched."""
    from mcp import types

    result = type("R", (), {"content": [types.TextContent(type="text", text="ok")]})()
    session = _FakeSession([], call_result=result)
    out = await _wrapper(_FakeToolDef("read_file"), session).execute(path="/x", n=2)
    assert out == "ok"
    assert session.calls == [("read_file", {"path": "/x", "n": 2})]


async def test_wrapper_empty_content_reports_no_output() -> None:
    result = type("R", (), {"content": []})()
    out = await _wrapper(_FakeToolDef("t"), _FakeSession([], call_result=result)).execute()
    assert out == "(no output)"


def test_wrapper_name_and_description() -> None:
    w = _wrapper(_FakeToolDef("read", description="reads things"), _FakeSession([]))
    assert w.name == "mcp_srv_read"
    assert w.description == "reads things"


def test_wrapper_falls_back_to_tool_name_when_description_missing() -> None:
    w = _wrapper(_FakeToolDef("read", description=None), _FakeSession([]))
    assert w.description == "read"


# ── Schema normalization ─────────────────────────────────────────────────


def test_schema_normalization_forces_object_type() -> None:
    out = _normalize_schema_for_openai({"type": "string"})
    assert out["type"] == "object"
    assert out["properties"] == {}
    assert out["required"] == []


def test_schema_normalization_flattens_top_level_anyof() -> None:
    out = _normalize_schema_for_openai(
        {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}}}, {"type": "null"}]}
    )
    assert out["type"] == "object"
    assert "a" in out["properties"]


def test_schema_normalization_drops_unsupported_top_level_keys() -> None:
    out = _normalize_schema_for_openai(
        {"type": "object", "properties": {}, "enum": ["x"], "const": 1, "not": {}}
    )
    assert "enum" not in out and "const" not in out and "not" not in out


def test_schema_normalization_handles_non_dict() -> None:
    assert _normalize_schema_for_openai("nonsense") == {"type": "object", "properties": {}}


# ── Deprecated transport warning (2026-07-28 spec) ───────────────────────


def _capture_warnings() -> tuple[list[str], int]:
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, sink_id


async def test_sse_transport_warns_that_it_is_deprecated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loguru import logger

    messages, sink_id = _capture_warnings()
    try:
        _install_fakes(monkeypatch, [_FakeToolDef("ping")])
        await _connect({"srv": MCPServerConfig(url="https://example.test/sse")})
    finally:
        logger.remove(sink_id)
    assert any("deprecated" in m and "streamableHttp" in m for m in messages)


async def test_streamable_http_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    from loguru import logger

    messages, sink_id = _capture_warnings()
    try:
        _install_fakes(monkeypatch, [_FakeToolDef("ping")])
        await _connect({"srv": MCPServerConfig(url="https://example.test/mcp")})
    finally:
        logger.remove(sink_id)
    assert not any("deprecated" in m for m in messages)


# ── One connection, many registries ──────────────────────────────────────


async def test_many_registries_share_a_single_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the per-registry reconnect: the engine injects MCP
    tools into default + direct + per-agent registries, and each extra registry
    used to mean another connection (another child process, for stdio)."""
    spy, _ = _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    registries = [ToolRegistry() for _ in range(3)]
    async with AsyncExitStack() as stack:
        await connect_mcp_servers(
            {"srv": MCPServerConfig(command="node")}, registries, stack
        )
    assert len(spy.stdio) == 1, "server must be connected once, not once per registry"
    for reg in registries:
        assert reg.tool_names == ["mcp_srv_ping"]


async def test_fanned_out_registries_share_the_same_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    a, b = ToolRegistry(), ToolRegistry()
    async with AsyncExitStack() as stack:
        await connect_mcp_servers({"srv": MCPServerConfig(command="node")}, [a, b], stack)
    assert a.get("mcp_srv_ping") is b.get("mcp_srv_ping")


async def test_single_registry_argument_still_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat: the old single-registry call shape must keep working."""
    _install_fakes(monkeypatch, [_FakeToolDef("ping")])
    reg = ToolRegistry()
    async with AsyncExitStack() as stack:
        await connect_mcp_servers({"srv": MCPServerConfig(command="node")}, reg, stack)
    assert reg.tool_names == ["mcp_srv_ping"]
