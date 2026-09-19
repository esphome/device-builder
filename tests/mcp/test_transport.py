"""JSON-RPC envelope, transport checks and method dispatch of the generic MCP server."""

from __future__ import annotations

from functools import partial
from typing import Any

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.mcp import McpServer, ToolRegistry
from esphome_device_builder.mcp.transport import (
    DEFAULT_PROTOCOL_VERSION,
    PROTOCOL_VERSION_HEADER,
    SUPPORTED_PROTOCOL_VERSIONS,
    JsonRpcErrorCode,
)

from ..conftest import rpc_post

_PATH = "/mcp"
_rpc = partial(rpc_post, path=_PATH)
_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
_CLIENT = {"capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}


def _init(version: Any) -> dict[str, Any]:
    return {"protocolVersion": version, **_CLIENT}


@pytest.fixture
async def client(aiohttp_client: AiohttpClient) -> Any:
    tools: ToolRegistry[dict[str, Any]] = ToolRegistry()

    @tools.tool("echo", "Echo the context and arguments.", {"text": {"type": "string"}}, ("text",))
    async def _echo(context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return {"context": context, "text": args["text"]}

    server = McpServer("Test Server", "1.2.3", tools)

    async def handle(request: web.Request) -> web.Response:
        return await server.handle({"site": "test"}, request)

    app = web.Application()
    app.router.add_post(_PATH, handle)
    return await aiohttp_client(app)


async def test_unsupported_protocol_version_header_is_400(client: Any) -> None:
    resp = await client.post(_PATH, json=_PING, headers={PROTOCOL_VERSION_HEADER: "2025-03-26"})
    assert resp.status == 400


@pytest.mark.parametrize("version", sorted(SUPPORTED_PROTOCOL_VERSIONS))
async def test_supported_protocol_version_header_passes(client: Any, version: str) -> None:
    reply = await _rpc(client, method="ping", headers={PROTOCOL_VERSION_HEADER: version})
    assert reply["result"] == {}


async def test_non_json_content_type_is_415(client: Any) -> None:
    resp = await client.post(_PATH, data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    assert resp.status == 415


async def test_parse_error(client: Any) -> None:
    resp = await client.post(_PATH, data=b"{nope", headers={"Content-Type": "application/json"})
    assert await resp.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": JsonRpcErrorCode.PARSE_ERROR, "message": "Parse error"},
    }


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([_PING], id="batch"),
        pytest.param({**_PING, "jsonrpc": "1.0"}, id="wrong_version"),
        pytest.param({"jsonrpc": "2.0", "id": 1}, id="missing_method"),
        pytest.param("ping", id="string"),
        pytest.param({**_PING, "id": None}, id="null_id"),
        pytest.param({**_PING, "id": True}, id="bool_id"),
        pytest.param({**_PING, "id": [1]}, id="array_id"),
        pytest.param({**_PING, "id": {"a": 1}}, id="object_id"),
    ],
)
async def test_invalid_request_answers_with_null_id(client: Any, body: Any) -> None:
    resp = await client.post(_PATH, json=body)
    assert await resp.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": JsonRpcErrorCode.INVALID_REQUEST, "message": "Invalid request"},
    }


async def test_notification_is_202_with_empty_body(client: Any) -> None:
    resp = await client.post(_PATH, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status == 202
    assert await resp.read() == b""


@pytest.mark.parametrize("msg_id", ["abc", 7])
async def test_ping_echoes_id(client: Any, msg_id: Any) -> None:
    assert await _rpc(client, method="ping", msg_id=msg_id) == {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {},
    }


async def test_unknown_method(client: Any) -> None:
    reply = await _rpc(client, method="server/discover")
    assert reply["error"] == {
        "code": JsonRpcErrorCode.METHOD_NOT_FOUND,
        "message": "Method not found: server/discover",
    }


@pytest.mark.parametrize("params", [["x"], None], ids=["array", "null"])
async def test_params_must_be_an_object_when_present(client: Any, params: Any) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": params}
    resp = await client.post(_PATH, json=body)
    assert (await resp.json())["error"]["code"] == JsonRpcErrorCode.INVALID_PARAMS


@pytest.mark.parametrize("version", sorted(SUPPORTED_PROTOCOL_VERSIONS))
async def test_initialize_echoes_supported_version(client: Any, version: str) -> None:
    reply = await _rpc(client, method="initialize", params=_init(version))
    assert reply["result"]["protocolVersion"] == version


@pytest.mark.parametrize("requested", ["2025-03-26", "2099-01-01"])
async def test_initialize_counter_offers_the_newest_supported_version(
    client: Any, requested: str
) -> None:
    reply = await _rpc(client, method="initialize", params=_init(requested))
    assert reply["result"]["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
    assert max(SUPPORTED_PROTOCOL_VERSIONS) == DEFAULT_PROTOCOL_VERSION


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="empty"),
        pytest.param(_init(7), id="non_string_version"),
        pytest.param(_init(["2025-06-18"]), id="list_version"),
        pytest.param({"protocolVersion": "2025-06-18"}, id="no_client"),
        pytest.param({**_init("2025-06-18"), "capabilities": []}, id="capabilities_not_object"),
        pytest.param({**_init("2025-06-18"), "clientInfo": {"name": "x"}}, id="client_no_version"),
    ],
)
async def test_initialize_rejects_malformed_params(client: Any, params: dict[str, Any]) -> None:
    reply = await _rpc(client, method="initialize", params=params)
    assert reply["error"]["code"] == JsonRpcErrorCode.INVALID_PARAMS


async def test_initialize_advertises_tools_and_server_info(client: Any) -> None:
    result = (await _rpc(client, method="initialize", params=_init("2025-06-18")))["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"] == {"name": "Test Server", "version": "1.2.3"}


async def test_tools_list(client: Any) -> None:
    assert (await _rpc(client, method="tools/list"))["result"] == {
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
    reply = await _rpc(
        client, method="tools/call", params={"name": "echo", "arguments": {"text": "hi"}}
    )
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
        pytest.param({"name": "echo", "arguments": None}, id="arguments_null"),
    ],
)
async def test_tools_call_invalid_params(client: Any, params: dict[str, Any]) -> None:
    assert (await _rpc(client, method="tools/call", params=params))["error"][
        "code"
    ] == JsonRpcErrorCode.INVALID_PARAMS


async def test_tools_call_arguments_default_to_empty_when_omitted(client: Any) -> None:
    reply = await _rpc(client, method="tools/call", params={"name": "echo"})
    assert reply["result"]["isError"] is True
    assert "Missing required argument: text" in reply["result"]["content"][0]["text"]
