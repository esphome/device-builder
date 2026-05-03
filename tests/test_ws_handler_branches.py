"""Coverage for ``websocket_handler`` branches that need a real aiohttp client.

Two paths the small unit-tests in ``test_ws_dispatch_branches.py``
can't reach because they live inside the request handler:

- Bearer-token pre-authentication (lines 261-266 of ``api/ws.py``).
- Invalid-JSON inside the message loop (lines 289-294).

Both require driving ``websocket_handler`` end-to-end through aiohttp,
so they live in their own file.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import WSMsgType, web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.models import ErrorCode


def _make_settings(*, using_password: bool) -> MagicMock:
    settings = MagicMock()
    settings.using_password = using_password
    settings.port = 6052
    settings.on_ha_addon = False
    settings.trusted_domains = []
    return settings


async def _connect_and_drain_server_info(client: Any, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
    """Open the WS and return ``(ws, server_info_dict)``.

    The handler always pushes a ``ServerInfoMessage`` first; tests
    inspect it to verify ``requires_auth`` was set correctly by the
    pre-auth path.
    """
    ws = await client.ws_connect("/ws", **kwargs)
    msg = await ws.receive(timeout=2.0)
    return ws, msg.json()


async def test_bearer_token_with_valid_session_pre_authenticates(
    aiohttp_client: AiohttpClient,
) -> None:
    """A valid ``Authorization: Bearer ...`` header skips the in-band auth handshake.

    Pin the bearer-validation success path: the session store
    returns a hit, the connection is marked pre-authenticated, and
    the ``ServerInfoMessage`` is sent with ``requires_auth=False``.
    Used by HA integration / CLI tools that don't speak the
    in-band ``auth/login`` protocol.
    """
    session = MagicMock()
    session.token = "session-token"

    auth = MagicMock()
    auth.session_store = MagicMock()
    auth.session_store.validate = AsyncMock(return_value=session)

    device_builder = MagicMock()
    device_builder.settings = _make_settings(using_password=True)
    device_builder.auth = auth
    device_builder.command_handlers = {}

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = False
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)

    ws, info = await _connect_and_drain_server_info(
        client, headers={"Authorization": "Bearer session-token"}
    )
    try:
        assert info["requires_auth"] is False
        # Validate was called with the extracted bearer.
        auth.session_store.validate.assert_awaited_once_with("session-token")
    finally:
        await ws.close()


async def test_bearer_token_with_invalid_session_falls_back_to_in_band_auth(
    aiohttp_client: AiohttpClient,
) -> None:
    """An invalid bearer leaves the connection unauthenticated.

    ``validate`` returning ``None`` is the typical "expired /
    revoked / wrong" outcome. The handler keeps going so the
    client can still drive the in-band ``auth/login`` flow — a
    blanket 403 here would force every misconfigured CLI client
    to reconnect after fixing its config.
    """
    auth = MagicMock()
    auth.session_store = MagicMock()
    auth.session_store.validate = AsyncMock(return_value=None)

    device_builder = MagicMock()
    device_builder.settings = _make_settings(using_password=True)
    device_builder.auth = auth
    device_builder.command_handlers = {}

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = False
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)

    ws, info = await _connect_and_drain_server_info(
        client, headers={"Authorization": "Bearer wrong-token"}
    )
    try:
        # Bearer validated but rejected — connection stays in the
        # un-authenticated bucket and the in-band handshake is
        # required.
        assert info["requires_auth"] is True
    finally:
        await ws.close()


async def test_invalid_json_message_returns_invalid_message_error(
    aiohttp_client: AiohttpClient,
) -> None:
    """A malformed payload over the wire surfaces as ``INVALID_MESSAGE``.

    Pin the dispatcher's ``loads(msg.data)`` ``except`` branch.
    Without it, a single garbage byte from a buggy client would
    crash the per-connection handler and disconnect everyone.
    """
    device_builder = MagicMock()
    device_builder.settings = _make_settings(using_password=False)
    device_builder.auth = MagicMock()
    device_builder.command_handlers = {}

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True  # skip in-band auth so the loop runs immediately
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)

    ws = await client.ws_connect("/ws")
    try:
        # Drain the ServerInfoMessage.
        await ws.receive(timeout=2.0)

        # Send garbage that ``json.loads`` rejects.
        await ws.send_str("not-json")

        msg = await ws.receive(timeout=2.0)
        assert msg.type == WSMsgType.TEXT
        payload = msg.json()
        assert payload["error_code"] == ErrorCode.INVALID_MESSAGE.value
        # Empty ``message_id`` because parsing failed before any
        # id could be extracted.
        assert payload["message_id"] == ""
    finally:
        await ws.close()
