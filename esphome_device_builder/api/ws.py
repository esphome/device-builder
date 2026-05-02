"""Multiplexed WebSocket API handler.

Single /ws endpoint. Dispatches commands to handlers registered on DeviceBuilder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import orjson
from aiohttp import WSMsgType, web
from esphome.const import __version__ as esphome_version

from ..constants import __version__
from ..controllers.auth import AuthError
from ..helpers.api import CommandError
from ..helpers.auth import extract_bearer_token
from ..models import (
    CommandMessage,
    ErrorCode,
    ErrorMessage,
    EventMessage,
    ResultMessage,
    ServerInfoMessage,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

# Commands a client may send before the authenticated flag is set.
_PRE_AUTH_COMMANDS = frozenset({"auth", "auth/login"})


class WebSocketClient:
    """A single WebSocket client connection."""

    def __init__(
        self,
        ws: web.WebSocketResponse,
        device_builder: DeviceBuilder,
        *,
        remote: str = "",
        authenticated: bool = False,
        token: str | None = None,
    ) -> None:
        self._ws = ws
        self.device_builder = device_builder
        self.remote = remote
        self._authenticated = authenticated
        self._token = token
        self._tasks: set[asyncio.Task] = set()
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._close_after_send: bool = False

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def token(self) -> str | None:
        return self._token

    def set_authenticated(self, token: str | None) -> None:
        """Mark this connection as authenticated and remember its token."""
        self._authenticated = True
        self._token = token

    def schedule_close(self) -> None:
        """Close the WebSocket after the current message is sent."""
        self._close_after_send = True

    async def send(self, data: dict[str, Any]) -> None:
        """Send a JSON message."""
        with contextlib.suppress(ConnectionResetError):
            await self._ws.send_str(orjson.dumps(data).decode())
        if self._close_after_send:
            await self._ws.close()

    async def send_result(self, message_id: str, result: Any = None) -> None:
        """Send a success result, serializing dataclass results automatically."""
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        msg = ResultMessage(message_id=message_id, result=result)
        await self.send(msg.to_dict())

    async def send_error(self, message_id: str, error_code: ErrorCode, details: str = "") -> None:
        """Send an error."""
        msg = ErrorMessage(message_id=message_id, error_code=error_code, details=details)
        await self.send(msg.to_dict())

    async def send_event(self, message_id: str, event: str, data: Any = None) -> None:
        """Send a streaming event."""
        msg = EventMessage(message_id=message_id, event=event, data=data)
        await self.send(msg.to_dict())

    def create_task(self, coro: Any) -> asyncio.Task:
        """Create a tracked task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def register_stream(self, message_id: str, task: asyncio.Task) -> None:
        """Register a long-running task so ``cancel_stream`` can stop it.

        Streaming command handlers call this with their own ``asyncio.current_task()``
        so a later ``stop_stream`` (or any peer with the message id) can cancel them.
        Pair with ``unregister_stream`` in a ``finally`` block.
        """
        self._stream_tasks[message_id] = task

    def unregister_stream(self, message_id: str) -> None:
        """Drop a previously-registered stream entry. Safe to call twice."""
        self._stream_tasks.pop(message_id, None)

    def cancel_stream(self, message_id: str) -> bool:
        """Cancel a registered stream by its id. Returns True if cancelled."""
        task = self._stream_tasks.pop(message_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def cleanup(self) -> None:
        """Cancel all pending tasks."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _handle_command(self, raw: dict[str, Any]) -> None:
        """Parse and dispatch a command."""
        try:
            cmd = CommandMessage.from_dict(raw)
        except Exception:
            await self.send_error("", ErrorCode.INVALID_MESSAGE, "Invalid command format")
            return

        if not self._authenticated and cmd.command not in _PRE_AUTH_COMMANDS:
            await self.send_error(
                cmd.message_id,
                ErrorCode.NOT_AUTHENTICATED,
                "Authentication required",
            )
            return

        handler = self.device_builder.command_handlers.get(cmd.command)
        if handler is None:
            await self.send_error(
                cmd.message_id,
                ErrorCode.UNKNOWN_COMMAND,
                f"Unknown command: {cmd.command}",
            )
            return

        try:
            result = await handler(client=self, message_id=cmd.message_id, **cmd.args)
            await self.send_result(cmd.message_id, result)
        except AuthError as err:
            await self.send_error(cmd.message_id, err.code, err.message)
        except CommandError as err:
            # Deliberate user-facing failure raised by a handler; pass
            # the code + message through verbatim so the client can
            # show something actionable instead of "Command failed".
            await self.send_error(cmd.message_id, err.code, err.message)
        except Exception:
            _LOGGER.exception("Error handling command %s", cmd.command)
            await self.send_error(
                cmd.message_id,
                ErrorCode.INTERNAL_ERROR,
                f"Command failed: {cmd.command}",
            )


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    """Multiplexed WebSocket API endpoint."""
    device_builder: DeviceBuilder = request.app["device_builder"]
    settings = device_builder.settings
    trusted_site = bool(request.app.get("trusted_site", False))

    # Reject cross-origin browser connections on the password-gated public
    # site. CORS middleware doesn't apply to WebSockets, so without this a
    # malicious page could open /ws against a victim's dashboard. Clients
    # without an Origin header (CLI tools, HA integration) are unaffected.
    if settings.using_password and not trusted_site:
        origin = request.headers.get("Origin")
        if origin and not _origin_matches_host(origin, request.host):
            return web.Response(status=403, text="Cross-origin connection rejected")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    pre_authenticated = trusted_site or not settings.using_password
    token: str | None = None

    if not pre_authenticated:
        # Non-browser clients (HA integration, CLI tools) can authenticate
        # via Authorization header instead of the in-band protocol.
        bearer = extract_bearer_token(request.headers.get("Authorization", ""))
        if bearer:
            session = await device_builder.auth.session_store.validate(bearer)
            if session is not None:
                pre_authenticated = True
                token = session.token

    client = WebSocketClient(
        ws,
        device_builder,
        remote=request.remote or "",
        authenticated=pre_authenticated,
        token=token,
    )

    # Per-connection: trusted-site and bearer-pre-auth connections don't need
    # the in-band auth handshake, so the frontend skips the login prompt.
    info = ServerInfoMessage(
        server_version=__version__,
        esphome_version=esphome_version,
        port=settings.port,
        ha_addon=settings.on_ha_addon,
        requires_auth=(not pre_authenticated),
    )
    await client.send(info.to_dict())

    try:
        async for msg in ws:
            if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                try:
                    raw = orjson.loads(msg.data)
                except Exception:
                    await client.send_error("", ErrorCode.INVALID_MESSAGE, "Invalid JSON")
                    continue
                client.create_task(client._handle_command(raw))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        await client.cleanup()
        _LOGGER.debug("WebSocket client disconnected")

    return ws


def create_ws_routes() -> web.RouteTableDef:
    """Create the WebSocket route table."""
    routes = web.RouteTableDef()

    @routes.get("/ws")
    async def ws_route(request: web.Request) -> web.StreamResponse:
        return await websocket_handler(request)

    return routes


def _origin_matches_host(origin: str, request_host: str) -> bool:
    """Return True when *origin*'s host:port matches the request's Host header."""
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == request_host
