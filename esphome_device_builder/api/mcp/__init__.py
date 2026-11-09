"""``/api/mcp``: the Device Builder MCP endpoint, tools over the WS command table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import hdrs, web

from ...constants import __version__
from ...helpers.auth import extract_bearer_token, reject_untrusted_browser_request
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

    # The SPA catch-all would otherwise serve index.html for GET.
    @routes.get(MCP_PATH)
    async def mcp_get(_request: web.Request) -> web.Response:
        return web.Response(status=405, headers={"Allow": "POST"})

    return routes


async def handle_post(request: web.Request) -> web.Response:
    """Apply the ``/ws`` Origin/Host gate and the ingress bearer, then answer the request."""
    # cors_middleware runs after the handler, so it cannot stop a cross-origin write.
    if (rejected := reject_untrusted_browser_request(request)) is not None:
        return rejected
    if (rejected := _reject_invalid_ingress_bearer(request)) is not None:
        return rejected
    return await _SERVER.handle(request.app["device_builder"], request)


def _reject_invalid_ingress_bearer(request: web.Request) -> web.Response | None:
    """401 a supervisor-channel request whose bearer isn't the ``--mcp-token-file`` token."""
    settings = request.app["device_builder"].settings
    if not settings.mcp_token or not request.app.get("supervisor_channel"):
        return None
    authorization = request.headers.get(hdrs.AUTHORIZATION)
    # Home Assistant before 2026.12 sends no credential, so a missing header still
    # passes on the peer guard alone; this goes away once the bearer is mandatory.
    if authorization is None:
        return None
    token = extract_bearer_token(authorization)
    if token is not None and settings.check_mcp_token(token):
        return None
    return web.Response(
        status=401, text="Invalid bearer token", headers={hdrs.WWW_AUTHENTICATE: "Bearer"}
    )
