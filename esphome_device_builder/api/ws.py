"""Multiplexed WebSocket API handler.

Single /ws endpoint. Dispatches commands to handlers registered on DeviceBuilder.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlsplit

from aiohttp import WSMsgType, web
from esphome.const import __version__ as esphome_version

from ..constants import __version__
from ..controllers.auth import AuthError
from ..helpers.api import CommandError
from ..helpers.auth import extract_bearer_token
from ..helpers.event_bus import StreamBackpressureError
from ..helpers.json import dumps_str, loads
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

# Server-side WebSocket ping interval. aiohttp's default is ``None``
# (no heartbeat) — without one, idle clients behind NAT / Cloudflare
# / nginx (whose default ``proxy_read_timeout`` is 60s) silently drop
# without either side noticing, and the dashboard sits showing stale
# data until the user reloads. 30s matches the legacy Tornado
# dashboard's ``websocket_ping_interval``.
_WS_HEARTBEAT_SECONDS = 30.0


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
            await self._ws.send_json(data, dumps=dumps_str)
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
        except StreamBackpressureError as err:
            # State-tracking stream exhausted its bounded queue. Send
            # the error so the client knows why and schedule the WS
            # close — the client reconnects, calls ``subscribe_events``
            # again, and gets a fresh ``initial_state`` snapshot. This
            # is the only correct recovery: the alternatives are silent
            # data loss (UI permanently stale) or unbounded memory
            # growth (OOM).
            #
            # ``schedule_close`` MUST run *before* ``send_error`` —
            # ``send`` only closes the socket when the flag is already
            # set when a message is being written. Setting it after
            # the error has been written would leave the connection
            # open with the handler task already gone, so the frontend
            # would stop receiving events but never get the forced
            # reconnect this branch is meant to provoke.
            _LOGGER.warning("Stream backpressure on %s: %s", cmd.command, err)
            self.schedule_close()
            await self.send_error(cmd.message_id, ErrorCode.INTERNAL_ERROR, str(err))
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
        # Both Origin / Host gates apply only to requests that
        # carry an ``Origin`` header — browser-driven WebSocket
        # connections always set it (spec-mandated for any WS
        # opening handshake), so any DNS-rebinding attack lands
        # here. CLI tools / HA integration / direct ``websockets``
        # clients omit Origin and skip both checks; the existing
        # bearer-token / in-band auth gate is doing the work for
        # them. Without this gate, an operator who sets
        # ``trusted_domains`` to harden against rebinding would
        # also lock out their HA integration.
        if origin:
            # Cross-origin acceptance gate: the Origin must equal
            # Host OR the Origin's hostname must be in the
            # operator-supplied trusted-domains allowlist. Without
            # the allowlist branch, reverse-proxy deployments where
            # Origin is ``https://dashboard.example.com`` but Host
            # is the upstream ``localhost:6052`` lose dashboard
            # access entirely.
            if not _origin_matches_host(origin, request.host) and not _origin_in_allowlist(
                origin, settings.trusted_domains
            ):
                return web.Response(status=403, text="Cross-origin connection rejected")
            # Defense-in-depth Host allowlist. Empty list = not
            # configured = pass through. When set, the request's
            # Host must be one of the trusted domains — mitigates
            # DNS-rebinding on top of the auth + per-IP rate limit
            # chain.
            if not _host_in_allowlist(request.host, settings.trusted_domains):
                return web.Response(status=403, text="Host not in trusted-domains allowlist")

    ws = web.WebSocketResponse(heartbeat=_WS_HEARTBEAT_SECONDS)
    await ws.prepare(request)

    pre_authenticated = trusted_site or not settings.using_password
    token: str | None = None

    if not pre_authenticated:
        # Non-browser clients (HA integration, CLI tools) can authenticate
        # via Authorization header instead of the in-band protocol.
        bearer = extract_bearer_token(request.headers.get("Authorization", ""))
        # ``device_builder.auth`` is typed ``AuthController | None``
        # for the pre-``start()`` window where the controller hasn't
        # been wired yet. By the time the WS handler runs ``start()``
        # has populated it, but ``assert`` is stripped under
        # ``python -O`` (see ``DeviceBuilder._install_default_executor``
        # for the rationale we follow elsewhere) — guard explicitly
        # and bind a local for narrowing instead.
        auth = device_builder.auth
        if bearer and auth is not None:
            session = await auth.session_store.validate(bearer)
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
        # CLOSE/ERROR exit via aiohttp's __anext__ → StopAsyncIteration; no explicit branch needed.
        async for msg in ws:
            if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                try:
                    raw = loads(msg.data)
                except Exception:
                    await client.send_error("", ErrorCode.INVALID_MESSAGE, "Invalid JSON")
                    continue
                client.create_task(client._handle_command(raw))
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


def _origin_in_allowlist(origin: str, allowlist: list[str]) -> bool:
    """Return True when ``origin``'s hostname is in the allowlist.

    Used by the cross-origin acceptance gate: reverse-proxy
    deployments where Origin is ``https://dashboard.example.com``
    but Host is ``localhost:6052`` (proxy upstream) need the
    operator-supplied ``ESPHOME_TRUSTED_DOMAINS`` allowlist to
    accept the cross-origin handshake.

    The allowlist match is on the Origin URL's hostname (port and
    scheme stripped), case-insensitive. A bare hostname entry like
    ``dashboard.example.com`` matches an Origin of
    ``https://Dashboard.Example.com`` regardless of port; an entry
    of ``[::1]`` matches ``http://[::1]:6052``.

    ``"*"`` matches anything (escape hatch for operators who set
    the env var without a specific host list).
    """
    if not allowlist:
        return False
    if "*" in allowlist:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    return any(_normalize_host(entry) == hostname for entry in allowlist)


def _normalize_host(host: str) -> str:
    """Lower-case ``host`` and strip the port + IPv6 brackets, if any.

    HTTP ``Host`` headers carry IPv6 addresses bracket-wrapped
    (``[::1]:6052``); naive ``split(":", 1)`` would chop the first
    segment of the address. ``urlsplit("//" + host).hostname``
    handles both shapes (IPv4 / hostname:port and ``[ipv6]:port``)
    and returns the unbracketed lowercase hostname.

    There's one edge case ``urlsplit`` mis-handles: a bare IPv6
    address typed *without* brackets (operator's allowlist entry
    of ``fe80::1`` rather than ``[fe80::1]``) — ``urlsplit``
    parses the leading ``fe80`` as the host and ``:1`` as the
    port. Short-circuit those via ``ipaddress.ip_address`` before
    falling through to the URL-parser branch. Bracketed Host
    headers go straight to ``urlsplit`` which handles them
    correctly. Falls back to the input verbatim when ``urlsplit``
    returns nothing usable (malformed Host header).
    """
    stripped = host.strip()
    if not stripped.startswith("["):
        try:
            ipaddress.ip_address(stripped)
        except ValueError:
            pass
        else:
            return stripped.lower()
    try:
        hostname = urlsplit(f"//{stripped}").hostname
    except ValueError:
        hostname = None
    if hostname is None:
        return stripped.lower()
    return hostname.lower()


def _host_in_allowlist(request_host: str, allowlist: list[str]) -> bool:
    """Return True when ``request_host`` is permitted by ``allowlist``.

    ``allowlist`` is the operator-supplied ``--trusted-domains`` /
    ``$ESPHOME_TRUSTED_DOMAINS`` list — empty means "no allowlist,
    anything goes" and the caller skips the check entirely.

    Both ``request_host`` and each allowlist entry go through
    ``_normalize_host`` (lower-case, port stripped, IPv6 brackets
    stripped). ``DashboardSettings.parse_args`` strips whitespace
    and lower-cases the entries on load but does NOT canonicalise
    bracket / port shape, so an entry of ``[::1]`` and a Host
    header of ``[::1]:6052`` (or an un-bracketed ``::1``) all
    end up normalised to ``::1`` here and compare equal.

    The literal ``"*"`` is an explicit "match anything" escape hatch
    for operators who want to record the config knob is set without
    restricting hosts (handy for split-hostname proxy setups where
    the Host header varies per request and the existing Origin/Host
    equality + auth chain is doing the work).

    Defense in depth on top of the existing Origin/Host equality
    check + per-IP-rate-limited ``auth/login``.
    """
    if not allowlist:
        return True
    if "*" in allowlist:
        return True
    normalised = _normalize_host(request_host)
    return any(_normalize_host(entry) == normalised for entry in allowlist)
