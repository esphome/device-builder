"""Fixtures and helpers shared by the MCP endpoint and tool suites."""

from __future__ import annotations

import json
from functools import partial
from typing import Any

import pytest
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api.mcp import MCP_PATH
from esphome_device_builder.controllers.components import ComponentCatalog

from ..conftest import (
    MakeSettingsFactory,
    McpStubDeviceBuilder,
    make_mcp_app,
    rpc_post,
)

mcp_rpc = partial(rpc_post, path=MCP_PATH)


async def mcp_call(client: Any, name: str, arguments: dict[str, Any] | None = None) -> tuple:
    """Return ``(is_error, text)`` for one ``tools/call``."""
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    result = (await mcp_rpc(client, method="tools/call", params=params))["result"]
    return result["isError"], result["content"][0]["text"]


async def mcp_call_json(client: Any, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Return a successful tool call's text parsed as JSON."""
    is_error, text = await mcp_call(client, name, arguments)
    assert not is_error, text
    return json.loads(text)


def validate_stub(frames: list[tuple[str, Any]]) -> Any:
    """Build a ``devices/validate`` stand-in that emits *frames*."""

    async def validate(*, client: Any, message_id: str, configuration: str) -> None:
        for event, data in frames:
            await client.send_event(message_id, event, data)

    return validate


@pytest.fixture
def mcp_db(make_settings: MakeSettingsFactory) -> McpStubDeviceBuilder:
    return McpStubDeviceBuilder(make_settings())


@pytest.fixture
async def mcp_client(mcp_db: McpStubDeviceBuilder, aiohttp_client: AiohttpClient) -> Any:
    return await aiohttp_client(make_mcp_app(mcp_db))


@pytest.fixture
def mcp_catalog_db(
    mcp_db: McpStubDeviceBuilder, session_component_catalog: ComponentCatalog
) -> McpStubDeviceBuilder:
    mcp_db.components = session_component_catalog
    mcp_db.command_handlers["components/get_component_bodies"] = (
        session_component_catalog.get_component_bodies
    )
    return mcp_db
