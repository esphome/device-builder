"""``/api/mcp``: the Device Builder MCP endpoint, tools over the WS command table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from ...constants import __version__
from ...helpers.origin import host_in_allowlist, request_origin_allowed
from ...mcp import McpServer
from .tools import TOOLS

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder

MCP_PATH = "/api/mcp"
SERVER_NAME = "ESPHome Device Builder"

_SERVER: McpServer[DeviceBuilder] = McpServer(SERVER_NAME, __version__, TOOLS)


def create_mcp_routes() -> web.RouteTableDef:
    """Create the ``/api/mcp`` route table: POST only, with an explicit GET 405."""
    routes = web.RouteTableDef()
    routes.post(MCP_PATH)(handle_post)

    # Without this the GET falls through to the SPA catch-all and serves index.html.
    @routes.get(MCP_PATH)
    async def mcp_get(_request: web.Request) -> web.Response:
        return web.Response(status=405, headers={"Allow": "POST"})

    return routes


async def handle_post(request: web.Request) -> web.Response:
    """Gate a browser-originated POST like the ``/ws`` handshake, then answer the JSON-RPC."""
    db: DeviceBuilder = request.app["device_builder"]
    # cors_middleware runs after the handler, so it cannot stop a cross-origin write.
    origin = request.headers.get("Origin")
    if origin and not request.app.get("trusted_site", False):
        if not request_origin_allowed(origin, request.host, db.settings.trusted_domains):
            return web.Response(status=403, text="Cross-origin request rejected")
        if not host_in_allowlist(request.host, db.settings.trusted_domains):
            return web.Response(status=403, text="Host not in trusted-domains allowlist")
    return await _SERVER.handle(db, request)
