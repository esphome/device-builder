"""JSON-RPC 2.0 envelope and MCP method dispatch for one stateless HTTP POST."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from ..helpers.json import JSONDecodeError, json_response, loads
from .tools import ToolRegistry

# Revisions before 2025-06-18 require JSON-RPC batching, which handle() rejects.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-11-25"})
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"

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
    """Tools-only MCP server answering one JSON-RPC request per POST."""

    def __init__(self, name: str, version: str, tools: ToolRegistry[ContextT]) -> None:
        self._server_info = {"name": name, "version": version}
        self._tools = tools
        self._methods: dict[str, Callable[[ContextT, dict[str, Any]], Awaitable[Any]]] = {
            "initialize": self._initialize,
            "ping": self._ping,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }

    async def handle(self, context: ContextT, request: web.Request) -> web.Response:
        """Answer one POST; a notification gets an empty 202."""
        rejected = _transport_check(request)
        if rejected is not None:
            return rejected
        try:
            msg = loads(await request.read())
        except JSONDecodeError:
            return _error_response(None, PARSE_ERROR, "Parse error")
        if not _is_valid_message(msg):
            return _error_response(None, INVALID_REQUEST, "Invalid request")
        if "id" not in msg:
            return web.Response(status=202)
        try:
            result = await self._dispatch(context, msg)
        except _RpcError as err:
            return _error_response(msg["id"], err.code, str(err))
        return json_response({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    async def _dispatch(self, context: ContextT, msg: dict[str, Any]) -> Any:
        handler = self._methods.get(msg["method"])
        if handler is None:
            raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {msg['method']}")
        return await handler(context, _object_param(msg, "params"))

    async def _initialize(self, _context: ContextT, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        return {
            "protocolVersion": (
                requested
                if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
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
        arguments = _object_param(params, "arguments")
        return await self._tools.call(context, name, arguments)


def _transport_check(request: web.Request) -> web.Response | None:
    """Reject an unsupported protocol-version header (400) or a non-JSON body (415)."""
    version = request.headers.get(PROTOCOL_VERSION_HEADER)
    if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
        return web.Response(status=400, text=f"Unsupported {PROTOCOL_VERSION_HEADER}")
    # Forces a CORS preflight; a text/plain simple request never reaches a tool.
    if request.content_type != "application/json":
        return web.Response(status=415, text="Content-Type must be application/json")
    return None


def _is_valid_message(msg: Any) -> bool:
    """Check for a JSON-RPC 2.0 object with a string method and a string or int id, if any."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return False
    if not isinstance(msg.get("method"), str):
        return False
    msg_id = msg.get("id")
    return "id" not in msg or (isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool))


def _object_param(container: dict[str, Any], key: str) -> dict[str, Any]:
    """Read the object at *key*; an omitted key is ``{}`` but any other value is INVALID_PARAMS."""
    if key not in container:
        return {}
    value = container[key]
    if not isinstance(value, dict):
        raise _RpcError(INVALID_PARAMS, f"{key} must be an object")
    return value


def _error_response(msg_id: Any, code: int, message: str) -> web.Response:
    """Build a JSON-RPC error reply (HTTP 200)."""
    return json_response(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    )
