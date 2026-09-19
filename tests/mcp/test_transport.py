"""JSON-RPC envelope and method dispatch of the generic MCP server."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.mcp import (
    DEFAULT_PROTOCOL_VERSION,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpServer,
    ToolRegistry,
)

# What the mcp SDK's handshake accepts back from ``initialize``; batching-era revisions omitted.
_HANDSHAKE_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def _make_server() -> McpServer[dict[str, Any]]:
    tools: ToolRegistry[dict[str, Any]] = ToolRegistry()

    @tools.tool("echo", "Echo the context and arguments.", {"text": {"type": "string"}}, ("text",))
    async def _echo(context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return {"context": context, "text": args["text"]}

    return McpServer("Test Server", "1.2.3", tools)


@pytest.fixture
async def client(aiohttp_client: AiohttpClient) -> Any:
    server = _make_server()

    async def handle(request: web.Request) -> web.Response:
        return await server.handle({"site": "test"}, await request.read())

    app = web.Application()
    app.router.add_post("/mcp", handle)
    return await aiohttp_client(app)


async def _rpc(client: Any, method: str, params: Any = None, *, msg_id: Any = 1) -> dict:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    resp = await client.post("/mcp", json=body)
    assert resp.status == 200
    assert resp.content_type == "application/json"
    return await resp.json()


async def test_parse_error(client: Any) -> None:
    resp = await client.post("/mcp", data=b"{nope")
    assert await resp.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": PARSE_ERROR, "message": "Parse error"},
    }


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], id="batch"),
        pytest.param({"jsonrpc": "1.0", "id": 1, "method": "ping"}, id="wrong_version"),
        pytest.param({"jsonrpc": "2.0", "id": 1}, id="missing_method"),
        pytest.param("ping", id="string"),
    ],
)
async def test_invalid_request(client: Any, body: Any) -> None:
    resp = await client.post("/mcp", json=body)
    assert (await resp.json())["error"]["code"] == INVALID_REQUEST


async def test_notification_is_202_with_empty_body(client: Any) -> None:
    resp = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status == 202
    assert await resp.read() == b""


@pytest.mark.parametrize("msg_id", [None, "abc", 7])
async def test_ping_echoes_id(client: Any, msg_id: Any) -> None:
    assert await _rpc(client, "ping", msg_id=msg_id) == {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {},
    }


async def test_unknown_method(client: Any) -> None:
    reply = await _rpc(client, "server/discover")
    assert reply["error"]["code"] == METHOD_NOT_FOUND
    assert "server/discover" in reply["error"]["message"]


async def test_params_must_be_an_object(client: Any) -> None:
    assert (await _rpc(client, "ping", ["x"]))["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("version", sorted(SUPPORTED_PROTOCOL_VERSIONS))
async def test_initialize_echoes_supported_version(client: Any, version: str) -> None:
    reply = await _rpc(client, "initialize", {"protocolVersion": version})
    assert reply["result"]["protocolVersion"] == version


@pytest.mark.parametrize("requested", ["2025-03-26", "2099-01-01", None, 7])
async def test_initialize_falls_back_to_default_version(client: Any, requested: Any) -> None:
    reply = await _rpc(client, "initialize", {"protocolVersion": requested})
    assert reply["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION


def test_supported_versions_are_handshake_versions() -> None:
    assert DEFAULT_PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS
    assert SUPPORTED_PROTOCOL_VERSIONS <= _HANDSHAKE_VERSIONS


async def test_initialize_advertises_tools_and_server_info(client: Any) -> None:
    result = (await _rpc(client, "initialize", {}))["result"]
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {"name": "Test Server", "version": "1.2.3"}


async def test_tools_list(client: Any) -> None:
    assert (await _rpc(client, "tools/list"))["result"] == {
        "tools": [
            {
                "name": "echo",
                "description": "Echo the context and arguments.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ]
    }


async def test_tools_call_passes_context_and_arguments(client: Any) -> None:
    reply = await _rpc(client, "tools/call", {"name": "echo", "arguments": {"text": "hi"}})
    assert reply["result"] == {
        "content": [{"type": "text", "text": '{"context":{"site":"test"},"text":"hi"}'}],
        "isError": False,
    }


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"name": "nope"}, id="unknown_tool"),
        pytest.param({"name": 5}, id="non_string_name"),
        pytest.param({"name": "echo", "arguments": ["x"]}, id="arguments_not_object"),
    ],
)
async def test_tools_call_invalid_params(client: Any, params: dict[str, Any]) -> None:
    assert (await _rpc(client, "tools/call", params))["error"]["code"] == INVALID_PARAMS


async def test_tools_call_arguments_default_to_empty(client: Any) -> None:
    reply = await _rpc(client, "tools/call", {"name": "echo", "arguments": None})
    assert reply["result"]["isError"] is True
    assert "Missing required argument: text" in reply["result"]["content"][0]["text"]
