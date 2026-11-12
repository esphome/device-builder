"""``/api/mcp``: the Device Builder MCP endpoint, tools over the WS command table."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import hdrs, web

from ...constants import __version__
from ...helpers.auth import extract_bearer_token, reject_untrusted_browser_request
from ...mcp import McpServer
from .tools import TOOLS

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

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
    """401 supervisor-channel requests without the bearer token; all of them if no token is set."""
    if not request.app.get("supervisor_channel"):
        return None
    token = extract_bearer_token(request.headers.get(hdrs.AUTHORIZATION, ""))
    if token is not None and request.app["device_builder"].settings.check_mcp_token(token):
        return None
    _LOGGER.debug("Rejecting MCP request without a valid bearer token from %s", request.remote)
    return web.Response(
        status=401,
        text="Missing or invalid bearer token",
        headers={hdrs.WWW_AUTHENTICATE: "Bearer"},
    )
