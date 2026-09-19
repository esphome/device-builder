"""The official ``mcp`` SDK client against Device Builder's endpoint and tools."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.api.mcp import MCP_PATH, SERVER_NAME
from esphome_device_builder.api.mcp.tools import TOOLS
from tests.conftest import MakeSettingsFactory, McpStubDeviceBuilder, make_mcp_app

mcp = pytest.importorskip("mcp")
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.types import TextContent  # noqa: E402


@pytest.fixture
async def server_url(aiohttp_server: Any, make_settings: MakeSettingsFactory) -> str:
    db = McpStubDeviceBuilder(make_settings())
    db.command_handlers["devices/get_config"] = AsyncMock(return_value="esphome:\n  name: k\n")
    return str((await aiohttp_server(make_mcp_app(db))).make_url(MCP_PATH))


async def test_sdk_client_reaches_the_device_builder_tools(server_url: str) -> None:
    async with (
        streamable_http_client(server_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        assert init.serverInfo.name == SERVER_NAME

        listed = await session.list_tools()
        assert [tool.name for tool in listed.tools] == list(TOOLS)

        result = await session.call_tool("get_config", {"configuration": "kitchen.yaml"})
        assert result.isError is False
        assert isinstance(result.content[0], TextContent)
        assert result.content[0].text == "esphome:\n  name: k\n"

        refused = await session.call_tool(
            "update_config", {"configuration": "secrets.yaml", "content": "wifi_password: x\n"}
        )
        assert refused.isError is True
        assert refused.content[0].text == (
            "invalid_args: secrets.yaml is read with get_config and changed with set_secret"
        )
