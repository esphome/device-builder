"""JSON-RPC 2.0 envelope and MCP method dispatch for one stateless HTTP POST."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from ..helpers.json import JSONDecodeError, json_response, loads
from .tools import ToolRegistry

# Echo allowlist only: the server behaves the same under every version here. Older
# revisions required JSON-RPC batching, which this server rejects, so they are not listed.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-11-25"})
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class _RpcError(Exception):
    """A JSON-RPC error reply: ``code`` plus the message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class McpServer[ContextT]:
    """Tools-only MCP server answering one JSON-RPC request per ``handle`` call."""

    def __init__(self, name: str, version: str, tools: ToolRegistry[ContextT]) -> None:
        self._server_info = {"name": name, "version": version}
        self._tools = tools
        self._methods: dict[str, Callable[[ContextT, dict[str, Any]], Awaitable[Any]]] = {
            "initialize": self._initialize,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }

    async def handle(self, context: ContextT, body: bytes) -> web.Response:
        """Answer one JSON-RPC request body; a notification gets an empty 202."""
        try:
            msg = loads(body)
        except JSONDecodeError:
            return _error_response(None, PARSE_ERROR, "Parse error")
        if (
            not isinstance(msg, dict)
            or msg.get("jsonrpc") != "2.0"
            or not isinstance(msg.get("method"), str)
        ):
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            return _error_response(msg_id, INVALID_REQUEST, "Invalid request")
        if "id" not in msg:
            return web.Response(status=202)
        try:
            result = await self._dispatch(context, msg["method"], msg.get("params"))
        except _RpcError as err:
            return _error_response(msg["id"], err.code, str(err))
        return json_response({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    async def _dispatch(self, context: ContextT, method: str, params: Any) -> Any:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise _RpcError(INVALID_PARAMS, "params must be an object")
        handler = self._methods.get(method)
        if handler is None:
            raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        return await handler(context, params)

    async def _initialize(self, _context: ContextT, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": (
                requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            ),
            "capabilities": {"tools": {}},
            "serverInfo": self._server_info,
        }

    async def _ping(self, _context: ContextT, _params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def _tools_list(self, _context: ContextT, _params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": self._tools.definitions()}

    async def _tools_call(self, context: ContextT, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in self._tools:
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        return await self._tools.call(context, name, arguments)


def _error_response(msg_id: Any, code: int, message: str) -> web.Response:
    """Build a JSON-RPC error reply (HTTP 200; the error rides in the body)."""
    return json_response(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    )
