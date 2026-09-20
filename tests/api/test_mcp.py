"""Coverage for the MCP endpoint route, identity and tool registry."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import jsonschema
import pytest
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api.mcp import MCP_PATH, SERVER_NAME
from esphome_device_builder.api.mcp.tools import TOOLS
from esphome_device_builder.constants import __version__
from esphome_device_builder.controllers.auth import AuthError
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.mcp import INTERNAL_ERROR, INVALID_ARGS
from esphome_device_builder.models import (
    ErrorCode,
)

from ..conftest import (
    MakeSettingsFactory,
    McpStubDeviceBuilder,
    make_mcp_app,
)
from .conftest import (
    mcp_call,
    mcp_rpc,
)

_INIT = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"},
}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "delete"])
async def test_non_post_is_405(mcp_client: Any, method: str) -> None:
    assert (await getattr(mcp_client, method)(MCP_PATH)).status == 405


async def test_route_wins_over_spa_catch_all(
    make_settings: MakeSettingsFactory, aiohttp_client: AiohttpClient
) -> None:
    pytest.importorskip("esphome_device_builder_frontend")
    real_db = DeviceBuilder(make_settings())
    mcp_client = await aiohttp_client(real_db.create_app(with_lifecycle=False))
    assert (await mcp_rpc(mcp_client, method="ping"))["result"] == {}
    assert (await mcp_client.get(MCP_PATH)).status == 405
    assert (await mcp_client.get("/some/deep/link")).status == 200


@pytest.mark.parametrize(
    ("tool", "arg", "default"),
    [
        ("set_secret", "overwrite", False),
        ("get_component", "include_advanced", False),
        ("get_automation_docs", "include_advanced", False),
    ],
)
def test_boolean_options_declare_their_default_in_the_schema(
    tool: str, arg: str, default: bool
) -> None:
    assert TOOLS[tool].schema["properties"][arg]["default"] is default


async def test_cross_origin_post_is_rejected_before_the_tool_runs(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder
) -> None:
    handler = AsyncMock(return_value=None)
    mcp_db.command_handlers["devices/update_config"] = handler
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "update_config", "arguments": {"configuration": "x", "content": "y"}},
    }
    resp = await mcp_client.post(MCP_PATH, json=body, headers={"Origin": "https://evil.example"})
    assert resp.status == 403
    handler.assert_not_awaited()


async def test_same_origin_post_is_allowed(mcp_client: Any) -> None:
    origin = f"http://{mcp_client.host}:{mcp_client.port}"
    assert (await mcp_rpc(mcp_client, method="ping", headers={"Origin": origin}))["result"] == {}


async def test_trusted_domains_allow_the_origin_and_gate_the_host(
    mcp_db: McpStubDeviceBuilder, aiohttp_client: AiohttpClient
) -> None:
    mcp_db.settings.trusted_domains = ["dashboard.local"]
    mcp_client = await aiohttp_client(make_mcp_app(mcp_db))
    headers = {"Origin": "https://dashboard.local"}
    # Origin is allowlisted but the request Host (127.0.0.1) is not.
    resp = await mcp_client.post(
        MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
    )
    assert resp.status == 403
    assert "trusted-domains" in await resp.text()
    mcp_db.settings.trusted_domains = ["*"]
    assert (await mcp_rpc(mcp_client, method="ping", headers=headers))["result"] == {}


async def test_trusted_site_skips_the_origin_gate(
    mcp_db: McpStubDeviceBuilder, aiohttp_client: AiohttpClient
) -> None:
    app = make_mcp_app(mcp_db)
    app["trusted_site"] = True
    mcp_client = await aiohttp_client(app)
    reply = await mcp_rpc(mcp_client, method="ping", headers={"Origin": "https://evil.example"})
    assert reply["result"] == {}


@pytest.mark.parametrize(("using_password", "status"), [(True, 401), (False, 200)])
async def test_password_gate(
    mcp_db: McpStubDeviceBuilder, aiohttp_client: AiohttpClient, using_password: bool, status: int
) -> None:
    mcp_db.settings.using_password = using_password
    mcp_client = await aiohttp_client(make_mcp_app(mcp_db, with_auth=True))
    resp = await mcp_client.post(MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status == status


# ---------------------------------------------------------------------------
# Server identity and tool registry
# ---------------------------------------------------------------------------


async def test_initialize_advertises_server_info(mcp_client: Any) -> None:
    result = (await mcp_rpc(mcp_client, method="initialize", params=_INIT))["result"]
    assert result["serverInfo"] == {"name": SERVER_NAME, "version": __version__}


async def test_tools_list_matches_registry_and_schemas_are_valid(mcp_client: Any) -> None:
    listed = (await mcp_rpc(mcp_client, method="tools/list"))["result"]["tools"]
    assert [tool["name"] for tool in listed] == list(TOOLS)
    for tool in listed:
        assert tool["description"]
        jsonschema.Draft202012Validator.check_schema(tool["inputSchema"])


def test_library_error_words_match_error_code() -> None:
    assert INVALID_ARGS == ErrorCode.INVALID_ARGS
    assert INTERNAL_ERROR == ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(CommandError(ErrorCode.NOT_FOUND, "gone"), "not_found: gone", id="command"),
        pytest.param(
            AuthError(ErrorCode.NOT_AUTHENTICATED, "no"), "not_authenticated: no", id="auth"
        ),
        pytest.param(
            FileNotFoundError("x"), "internal_error: Tool failed: get_config", id="not_user_facing"
        ),
    ],
)
async def test_handler_exceptions_become_tool_errors(
    mcp_client: Any, mcp_db: McpStubDeviceBuilder, exc: Exception, expected: str
) -> None:
    mcp_db.command_handlers["devices/get_config"] = AsyncMock(side_effect=exc)
    assert await mcp_call(mcp_client, "get_config", {"configuration": "k.yaml"}) == (True, expected)


async def test_missing_command_is_unavailable(mcp_client: Any) -> None:
    is_error, text = await mcp_call(mcp_client, "get_config", {"configuration": "k.yaml"})
    assert is_error
    assert text == "unavailable: devices/get_config is not available"
