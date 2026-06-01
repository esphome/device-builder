"""Coverage for ``websocket_handler`` branches that need a real aiohttp client.

Two paths the small unit-tests in ``test_ws_dispatch_branches.py``
can't reach because they live inside the request handler:

- Bearer-token pre-authentication: when ``settings.using_password``
  is on and the request carries an ``Authorization: Bearer ...``
  header, the handler validates the token via
  ``device_builder.auth.session_store.validate`` and either marks
  the connection pre-authenticated (token recorded for later
  ``auth/refresh`` / ``auth/logout``) or falls through to the
  in-band ``auth/login`` flow.
- The WS message loop's invalid-JSON branch: a frame whose body
  fails ``json.loads`` is answered with an ``INVALID_MESSAGE``
  error and the loop ``continue``-s — the connection survives so
  the client can recover instead of getting kicked.

Both require driving ``websocket_handler`` end-to-end through aiohttp,
so they live in their own file.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import WSMsgType, web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.api.ws import WebSocketClient
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
    returns a hit, the connection is marked pre-authenticated,
    and the ``ServerInfoMessage`` carries ``requires_auth=False``.
    Also pin ``client.token = session.token`` because later
    ``auth/refresh`` / ``auth/logout`` lookups key off it — a
    regression that left ``token=None`` on a bearer-pre-auth
    socket would still flip ``requires_auth`` correctly but
    silently break those endpoints. Used by HA integration / CLI
    tools that don't speak the in-band ``auth/login`` protocol.
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
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())

    # Capture the ``WebSocketClient`` instance the handler builds
    # so the test can inspect ``token`` / ``authenticated`` after
    # the bearer-pre-auth path runs.
    constructed: list[WebSocketClient] = []
    real_init = WebSocketClient.__init__

    def _capture_init(self: WebSocketClient, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        constructed.append(self)

    client = await aiohttp_client(app)

    with patch.object(WebSocketClient, "__init__", _capture_init):
        ws, info = await _connect_and_drain_server_info(
            client, headers={"Authorization": "Bearer session-token"}
        )
    try:
        assert info["requires_auth"] is False
        # Validate was called with the extracted bearer.
        auth.session_store.validate.assert_awaited_once_with("session-token")
        # Token was threaded onto the connection for downstream
        # ``auth/refresh`` / ``auth/logout`` lookups.
        assert len(constructed) == 1
        assert constructed[0].token == "session-token"
        assert constructed[0].authenticated is True
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

    Also pin that ``validate`` was actually awaited with the
    bearer string. Without that assertion, a regression that
    stopped reading the ``Authorization`` header (or skipped the
    validation call entirely) would still satisfy
    ``requires_auth=True`` and pass this test silently.
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
    ws_module.init_ws_app(app)
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
        # The handler actually consulted the session store with
        # the bearer string we sent.
        auth.session_store.validate.assert_awaited_once_with("wrong-token")
    finally:
        await ws.close()


async def test_invalid_json_message_returns_invalid_message_error(
    aiohttp_client: AiohttpClient,
) -> None:
    """A malformed payload over the wire surfaces as ``INVALID_MESSAGE``.

    Pin the dispatcher's ``loads(msg.data)`` ``except`` branch.
    Without it, a single garbage byte from a buggy client would
    tear down that client's connection mid-loop. The handler is
    per-connection so the blast radius is one client, but a
    spuriously-disconnecting CLI tool is still a debugging
    nightmare we'd rather pin down here.

    Three assertions, in order of importance:

    1. The error frame's ``details`` carry "Invalid JSON" — pins
       the JSON-parse branch specifically rather than any of the
       three ways the dispatcher can emit ``INVALID_MESSAGE``
       (the ``CommandMessage.from_dict`` failure path returns the
       same code with a different ``details`` string, and a
       handler that forwarded the raw text into ``_handle_command``
       would also satisfy a code-only check).
    2. The connection survives — sending a follow-up valid
       command and receiving its result proves the dispatcher
       ``continue``-d instead of breaking out of the message
       loop.
    """
    device_builder = MagicMock()
    device_builder.settings = _make_settings(using_password=False)
    device_builder.auth = MagicMock()

    async def _ping_handler(*, client: Any, message_id: str, **_kwargs: Any) -> dict[str, str]:
        return {"pong": "yes"}

    device_builder.command_handlers = {"ping": _ping_handler}

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True  # skip in-band auth so the loop runs immediately
    ws_module.init_ws_app(app)
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
        # Pin the JSON-parse branch specifically, not the
        # ``CommandMessage.from_dict`` failure (which returns the
        # same code with different details).
        assert "Invalid JSON" in payload["details"]

        # Connection still alive: send a valid command and
        # receive its result. If ``continue`` had been replaced
        # with ``break``, this round-trip would hang.
        await ws.send_json({"message_id": "after-bad", "command": "ping"})
        result = await ws.receive(timeout=2.0)
        assert result.type == WSMsgType.TEXT
        result_payload = result.json()
        assert result_payload["message_id"] == "after-bad"
        assert result_payload["result"] == {"pong": "yes"}
    finally:
        await ws.close()


async def test_server_info_ha_ingress_reflects_x_ingress_path_header(
    aiohttp_client: AiohttpClient,
) -> None:
    """``ha_ingress`` mirrors whether Supervisor's ``X-Ingress-Path`` header is present."""
    device_builder = MagicMock()
    device_builder.settings = _make_settings(using_password=False)
    device_builder.auth = MagicMock()
    device_builder.command_handlers = {}

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)

    ws, info = await _connect_and_drain_server_info(
        client, headers={"X-Ingress-Path": "/api/hassio_ingress/token"}
    )
    try:
        assert info["ha_ingress"] is True
    finally:
        await ws.close()

    ws, info = await _connect_and_drain_server_info(client)
    try:
        assert info["ha_ingress"] is False
    finally:
        await ws.close()
