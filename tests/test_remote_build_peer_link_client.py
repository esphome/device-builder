"""
Tests for the offloader-side peer-link Noise WS client (phase 4a-o part 2).

Two layers:

* End-to-end: stand up the receiver-side handler in-process via
  :func:`make_peer_link_handler` against an
  :class:`aiohttp.test_utils.TestServer`, then drive
  :func:`preview_pair` from the offloader side and assert the
  captured ``pin_sha256`` matches the receiver's actual identity.
* Error mapping: the various transport / handshake / decode
  failure modes all surface as :class:`PeerLinkClientError` so
  the WS-command layer can map them to a single
  ``UNAVAILABLE`` :class:`CommandError` without enumerating
  every cause.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build_peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.controllers.remote_build_peer_link_client import (
    PeerLinkClientError,
    _build_ws_url,
    drive_initiator_round_trip,
    preview_pair,
)
from esphome_device_builder.helpers.peer_link_identity import (
    get_or_create_peer_link_identity,
)
from esphome_device_builder.helpers.peer_link_noise import (
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from esphome_device_builder.models import PeerLinkIntent


def _make_controller(*, config_dir: Path) -> RemoteBuildController:
    db = MagicMock()
    db.devices = MagicMock()
    db.devices.zeroconf = None
    db._dashboard_advertiser = None
    db.settings = MagicMock()
    db.settings.config_dir = config_dir
    return RemoteBuildController(db)


@pytest.fixture
async def receiver_server(
    tmp_path: Path,
) -> AsyncGenerator[tuple[TestServer, RemoteBuildController, str], None]:
    """Spin up an in-process receiver. Yields (server, controller, expected_pin)."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    loop = asyncio.get_running_loop()
    identity = await loop.run_in_executor(None, get_or_create_peer_link_identity, tmp_path)

    app = web.Application()
    handler = await make_peer_link_handler(controller, tmp_path)
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server, controller, pin_sha256_for_pubkey(identity.public_bytes)
    finally:
        await server.close()
        await controller.stop()


# ---------------------------------------------------------------------------
# preview_pair — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_pair_returns_receivers_pin(
    receiver_server: tuple[TestServer, RemoteBuildController, str],
    tmp_path: Path,
) -> None:
    """The captured pin from the handshake matches the receiver's actual identity."""
    server, _, expected_pin = receiver_server
    initiator_priv = secrets.token_bytes(32)

    pin = await preview_pair(
        hostname="127.0.0.1",
        port=server.port,
        identity_priv=initiator_priv,
    )

    assert pin == expected_pin


@pytest.mark.asyncio
async def test_preview_pair_does_not_persist_state_on_receiver(
    receiver_server: tuple[TestServer, RemoteBuildController, str],
) -> None:
    """``intent="preview"`` returns ``OK`` without creating a peer row.

    Pin the contract that preview is read-only against the
    receiver's pairing state — the offloader runs preview before
    the user has decided whether to trust the receiver, so
    receiver-side bookkeeping must not happen yet.
    """
    server, controller, _ = receiver_server
    initiator_priv = secrets.token_bytes(32)

    await preview_pair(
        hostname="127.0.0.1",
        port=server.port,
        identity_priv=initiator_priv,
    )
    # No pair_request_received event fired (preview doesn't create rows).
    controller._db.bus.fire.assert_not_called()


# ---------------------------------------------------------------------------
# preview_pair — error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_pair_connection_refused_raises_client_error(
    tmp_path: Path,
    unused_tcp_port: int,
) -> None:
    """Connecting to a closed port raises :class:`PeerLinkClientError`."""
    initiator_priv = secrets.token_bytes(32)
    with pytest.raises(PeerLinkClientError, match="failed"):
        await preview_pair(
            hostname="127.0.0.1",
            port=unused_tcp_port,
            identity_priv=initiator_priv,
        )


@pytest.mark.asyncio
async def test_drive_initiator_round_trip_timeout_raises_client_error() -> None:
    """A hung TCP socket trips the WS handshake timeout, surfaced as PeerLinkClientError.

    Tests the shared driver directly via the ``timeout_seconds``
    kwarg rather than monkeypatching a module-level constant —
    the wrapper functions (preview_pair, future request_pair /
    poll_pair_status) all funnel through the driver, so the
    timeout contract stays under one test.
    """
    loop = asyncio.get_running_loop()
    # Bind a TCP socket that accepts connections but never speaks.
    server = await loop.create_server(asyncio.Protocol, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    initiator_priv = secrets.token_bytes(32)
    try:
        with pytest.raises(PeerLinkClientError):
            await drive_initiator_round_trip(
                hostname="127.0.0.1",
                port=port,
                identity_priv=initiator_priv,
                intent=PeerLinkIntent.PREVIEW,
                timeout_seconds=0.1,
            )
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_pair_rejects_garbage_post_handshake_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receiver sends a frame that decrypts to non-JSON → PeerLinkClientError.

    Stand up a custom WS handler that runs a real Noise XX
    responder for the 3 handshake messages but then writes a
    *plaintext* frame instead of a properly encrypted
    intent_response. The offloader's ``decrypt`` (or the JSON
    parse) on that frame should fail and surface as
    :class:`PeerLinkClientError` rather than escape uncaught.
    """
    receiver_priv = secrets.token_bytes(32)

    async def _faulty_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sess = PeerLinkNoiseSession.responder(receiver_priv)
        # msg1
        msg1 = await ws.receive_bytes()
        sess.read_handshake_message(msg1)
        # msg2
        await ws.send_bytes(sess.write_handshake_message(b""))
        # msg3
        msg3 = await ws.receive_bytes()
        sess.read_handshake_message(msg3)
        # Send a plaintext (non-Noise) frame so decrypt fails.
        await ws.send_bytes(b"this is not an encrypted frame")
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(PEER_LINK_PATH, _faulty_handler)
    server = TestServer(app)
    await server.start_server()
    initiator_priv = secrets.token_bytes(32)
    try:
        with pytest.raises(PeerLinkClientError, match="decode failed"):
            await preview_pair(
                hostname="127.0.0.1",
                port=server.port,
                identity_priv=initiator_priv,
            )
    finally:
        await server.close()


def test_build_ws_url_uses_plain_ws_scheme() -> None:
    """Peer-link runs over plain TCP; Noise XX provides transport security."""
    assert str(_build_ws_url("desk.local", 6055)) == "ws://desk.local:6055/remote-build/peer-link"


def test_build_ws_url_brackets_ipv6_literal() -> None:
    """Yarl auto-brackets IPv6 hostnames; an f-string approach would have garbled them."""
    assert str(_build_ws_url("::1", 6055)) == "ws://[::1]:6055/remote-build/peer-link"


def test_build_ws_url_rejects_pathological_host() -> None:
    """Yarl raises ``ValueError`` on path-injection attempts in the host position.

    The error message text is yarl's own and not part of our
    contract (could change between yarl versions); just assert
    the type. ``drive_initiator_round_trip`` catches this
    ``ValueError`` and maps it to ``PeerLinkClientError`` →
    ``UNAVAILABLE`` so a frontend that forwarded an unvalidated
    host gets a "couldn't reach receiver" toast rather than an
    internal-error stack trace; that path is covered by
    ``test_drive_initiator_round_trip_maps_pathological_host_to_client_error``.
    """
    with pytest.raises(ValueError):
        _build_ws_url("evil/path", 6055)


@pytest.mark.asyncio
async def test_drive_initiator_round_trip_maps_pathological_host_to_client_error() -> None:
    """A pathological host typed in the hostname field maps to PeerLinkClientError.

    yarl raises ``ValueError`` from ``_build_ws_url`` before
    any TCP connect; the driver catches it alongside the
    transport-failure tuple so the WS-command layer maps to
    ``UNAVAILABLE`` (transient, retry) instead of letting the
    raw ``ValueError`` escape as ``INTERNAL_ERROR``. Pin the
    contract: a frontend bug that forwards ``host:8080`` to
    ``hostname`` shouldn't crash the server.
    """
    initiator_priv = secrets.token_bytes(32)
    with pytest.raises(PeerLinkClientError, match="failed"):
        await drive_initiator_round_trip(
            hostname="host:8080",  # embedded port — yarl rejects
            port=6055,
            identity_priv=initiator_priv,
            intent=PeerLinkIntent.PREVIEW,
        )
