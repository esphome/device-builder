"""The official ``mcp`` SDK client (the line Home Assistant ships) against the server library."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from esphome_device_builder.mcp import McpServer, McpToolError, ToolRegistry
from esphome_device_builder.mcp.transport import SUPPORTED_PROTOCOL_VERSIONS

mcp = pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.exceptions import McpError  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

_PATH = "/mcp"


@pytest.fixture
async def server_url(aiohttp_server: Any) -> str:
    tools: ToolRegistry[dict[str, Any]] = ToolRegistry()

    @tools.tool("echo", "Echo the text.", {"text": {"type": "string"}}, ("text",))
    async def _echo(context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return {"site": context["site"], "text": args["text"]}

    @tools.tool("fail", "Always fails.")
    async def _fail(_context: dict[str, Any], _args: dict[str, Any]) -> None:
        raise McpToolError("not_found", "gone")

    server = McpServer("Test Server", "1.2.3", tools)

    async def handle(request: web.Request) -> web.Response:
        return await server.handle({"site": "test"}, request)

    app = web.Application()
    app.router.add_post(_PATH, handle)
    return str((await aiohttp_server(app)).make_url(_PATH))


async def test_sdk_client_session_round_trip(server_url: str) -> None:
    async with (
        streamable_http_client(server_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        assert init.protocolVersion in SUPPORTED_PROTOCOL_VERSIONS
        assert init.serverInfo.name == "Test Server"
        assert init.serverInfo.version == "1.2.3"
        assert init.capabilities.tools is not None

        listed = await session.list_tools()
        assert [tool.name for tool in listed.tools] == ["echo", "fail"]
        assert listed.tools[0].inputSchema["required"] == ["text"]

        result = await session.call_tool("echo", {"text": "hi"})
        assert isinstance(result, CallToolResult)
        assert result.isError is False
        assert isinstance(result.content[0], TextContent)
        assert result.content[0].text == '{"site":"test","text":"hi"}'

        rejected = await session.call_tool("echo", {"text": 5})
        assert rejected.isError is True
        assert "must be string" in rejected.content[0].text

        failed = await session.call_tool("fail", {})
        assert failed.isError is True
        assert failed.content[0].text == "not_found: gone"

        with pytest.raises(McpError, match="Unknown tool"):
            await session.call_tool("nope", {})
        with pytest.raises(McpError, match="Method not found"):
            await session.list_resources()
