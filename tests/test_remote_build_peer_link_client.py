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
import hashlib
import io
import secrets
import tarfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import WSMessage, WSMsgType, web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from noise.exceptions import NoiseInvalidMessage

from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build import controller as rb
from esphome_device_builder.controllers.remote_build import (
    peer_link_client as remote_build_peer_link_client,
)
from esphome_device_builder.controllers.remote_build.peer_link import (
    PEER_LINK_PATH,
    PeerLinkChannel,
    make_peer_link_handler,
)
from esphome_device_builder.controllers.remote_build.peer_link_client import (
    DownloadArtifactsError,
    DownloadArtifactsResult,
    PairStatusResult,
    PeerLinkClient,
    PeerLinkClientError,
    PeerLinkNoSessionError,
    RequestPairResult,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
    _build_ws_url,
    _DownloadArtifactsState,
    drive_initiator_round_trip,
    preview_pair,
    request_pair,
)
from esphome_device_builder.helpers import json as _json
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.peer_link_bundle import (
    chunk_bundle,
    compute_bundle_sha256,
    encode_chunk,
)
from esphome_device_builder.helpers.peer_link_identity import (
    get_or_create_peer_link_identity,
)
from esphome_device_builder.helpers.peer_link_noise import (
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from esphome_device_builder.models import (
    ErrorCode,
    EventType,
    IntentResponse,
    OffloaderJobStateChangedData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    PeerLinkIntent,
    PeerStatus,
    StoredPairing,
    StoredPeer,
)

from .conftest import (
    MakeSettingsFactory,
    cancel_and_drain,
    capture_events,
    make_remote_build_controller,
)


def _make_controller(*, config_dir: Path) -> RemoteBuildController:
    return make_remote_build_controller(config_dir=config_dir)


@pytest.fixture
async def receiver_server(
    tmp_path: Path,
) -> AsyncGenerator[tuple[TestServer, RemoteBuildController, str, bytes], None]:
    """Spin up an in-process receiver. Yields (server, controller, expected_pin, pub).

    The fourth element is the receiver's static X25519 pubkey
    bytes so tests constructing :class:`PeerLinkClient` can pass
    it as ``pinned_static_x25519_pub`` (the security pin-check
    added in 4a-o part 5 rejects the connect when the captured
    pubkey doesn't match this value).
    """
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
        yield (
            server,
            controller,
            pin_sha256_for_pubkey(identity.public_bytes),
            identity.public_bytes,
        )
    finally:
        await server.close()
        await controller.stop()


# ---------------------------------------------------------------------------
# preview_pair — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_pair_returns_receivers_pin(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    tmp_path: Path,
) -> None:
    """The captured pin from the handshake matches the receiver's actual identity."""
    server, _, expected_pin, _ = receiver_server
    initiator_priv = secrets.token_bytes(32)

    pin = await preview_pair(
        hostname="127.0.0.1",
        port=server.port,
        identity_priv=initiator_priv,
    )

    assert pin == expected_pin


@pytest.mark.asyncio
async def test_preview_pair_does_not_persist_state_on_receiver(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """``intent="preview"`` returns ``OK`` without creating a peer row.

    Pin the contract that preview is read-only against the
    receiver's pairing state — the offloader runs preview before
    the user has decided whether to trust the receiver, so
    receiver-side bookkeeping must not happen yet.
    """
    server, controller, _, _ = receiver_server
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
    await_pair_status) all funnel through the driver, so the
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


@pytest.mark.asyncio
async def test_preview_pair_non_ok_intent_response_raises_client_error() -> None:
    """Receiver's preview returns a non-OK intent_response → PeerLinkClientError.

    Preview's accept-set is just ``IntentResponse.OK``. Anything
    else (a future code we don't know, a deployment bug, a
    receiver that's mid-rotation) has to surface as a client
    error so the WS-command layer can map it to ``UNAVAILABLE``
    without leaking the raw response.
    """
    receiver_priv = secrets.token_bytes(32)

    async def _handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sess = PeerLinkNoiseSession.responder(receiver_priv)
        sess.read_handshake_message(await ws.receive_bytes())
        await ws.send_bytes(sess.write_handshake_message(b""))
        sess.read_handshake_message(await ws.receive_bytes())
        # Preview never gets ``rejected`` from a real receiver
        # (preview's responder branch unconditionally answers
        # OK), but a misbehaving deployment could; pin the
        # offloader-side rejection regardless.
        await ws.send_bytes(sess.encrypt(b'{"intent_response": "rejected"}'))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(PEER_LINK_PATH, _handler)
    server = TestServer(app)
    await server.start_server()
    initiator_priv = secrets.token_bytes(32)
    try:
        with pytest.raises(PeerLinkClientError, match="preview rejected"):
            await preview_pair(
                hostname="127.0.0.1",
                port=server.port,
                identity_priv=initiator_priv,
            )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_drive_initiator_round_trip_non_object_response_raises_client_error() -> None:
    """Receiver's response decrypts to a JSON value that isn't a dict → PeerLinkClientError.

    Defends against a wire-format slip where the receiver
    encrypts a list / scalar / null instead of the agreed
    ``{intent_response: ...}`` object. The driver has to refuse
    cleanly so the caller doesn't pass a malformed value to
    ``decoded.get(...)``.
    """
    receiver_priv = secrets.token_bytes(32)

    async def _handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sess = PeerLinkNoiseSession.responder(receiver_priv)
        sess.read_handshake_message(await ws.receive_bytes())
        await ws.send_bytes(sess.write_handshake_message(b""))
        sess.read_handshake_message(await ws.receive_bytes())
        # Valid Noise + valid JSON, but not the object shape.
        await ws.send_bytes(sess.encrypt(b'["surprise", 1, 2]'))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(PEER_LINK_PATH, _handler)
    server = TestServer(app)
    await server.start_server()
    initiator_priv = secrets.token_bytes(32)
    try:
        with pytest.raises(PeerLinkClientError, match="not a JSON object"):
            await drive_initiator_round_trip(
                hostname="127.0.0.1",
                port=server.port,
                identity_priv=initiator_priv,
                intent=PeerLinkIntent.PREVIEW,
            )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_drive_initiator_round_trip_missing_intent_response_raises_client_error() -> None:
    """Response object without an ``intent_response`` string field → PeerLinkClientError.

    Pin the contract: the driver has to refuse rather than pass
    a missing-key dict to the caller's accept-set check (which
    would silently mismatch every known response code).
    """
    receiver_priv = secrets.token_bytes(32)

    async def _handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sess = PeerLinkNoiseSession.responder(receiver_priv)
        sess.read_handshake_message(await ws.receive_bytes())
        await ws.send_bytes(sess.write_handshake_message(b""))
        sess.read_handshake_message(await ws.receive_bytes())
        # Object shape but the wrong keys.
        await ws.send_bytes(sess.encrypt(b'{"unrelated": "payload"}'))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(PEER_LINK_PATH, _handler)
    server = TestServer(app)
    await server.start_server()
    initiator_priv = secrets.token_bytes(32)
    try:
        with pytest.raises(PeerLinkClientError, match="missing 'intent_response'"):
            await drive_initiator_round_trip(
                hostname="127.0.0.1",
                port=server.port,
                identity_priv=initiator_priv,
                intent=PeerLinkIntent.PREVIEW,
            )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_drive_initiator_round_trip_handshake_not_complete_raises_client_error(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: ``remote_static_pub`` raising ``HandshakeNotCompleteError`` is mapped.

    Structurally unreachable from a black-box receiver — the
    property only raises when the handshake hasn't completed,
    and the driver only reaches that access after a successful
    decrypt + JSON-parse + intent_response check (all of which
    require a completed handshake). Replace the *initiator*
    factory with a subclass whose ``remote_static_pub`` always
    raises so the guard's mapping is exercised; the receiver's
    factory is untouched so the in-process handshake still
    completes normally. Without this guard, a future refactor
    that broke the post-handshake state (an upstream API change
    in noiseprotocol, a session that swallowed the captured
    pubkey) would surface as an uncaught exception instead of
    the documented ``PeerLinkClientError`` → ``UNAVAILABLE``
    shape.
    """
    server, _, _, _ = receiver_server
    initiator_priv = secrets.token_bytes(32)

    class _BrokenInitiator(PeerLinkNoiseSession):
        @property
        def remote_static_pub(self) -> bytes:
            raise HandshakeNotCompleteError("forced for test")

    real_initiator = PeerLinkNoiseSession.initiator

    def _broken_factory(identity_priv: bytes) -> PeerLinkNoiseSession:
        sess = real_initiator(identity_priv)
        sess.__class__ = _BrokenInitiator
        return sess

    # Patch the symbol the driver actually imports. Overriding only
    # ``initiator`` instances keeps the receiver-side ``responder``
    # handshake intact, while changing shared base-class behavior
    # such as ``remote_static_pub`` would affect both sides and fail
    # the test before the initiator's post-handshake access.
    monkeypatch.setattr(
        remote_build_peer_link_client.PeerLinkNoiseSession,
        "initiator",
        staticmethod(_broken_factory),
    )

    with pytest.raises(PeerLinkClientError, match="without capturing remote static pubkey"):
        await drive_initiator_round_trip(
            hostname="127.0.0.1",
            port=server.port,
            identity_priv=initiator_priv,
            intent=PeerLinkIntent.PREVIEW,
        )


@pytest.mark.asyncio
async def test_drive_initiator_round_trip_short_msg2_raises_noise_handshake_failed() -> None:
    """A msg2 too short to parse → ``NoiseValueError`` → PeerLinkClientError.

    The Noise read on msg2 raises out of :data:`NOISE_ERRORS`
    rather than the connect-time / decode-time tuple. Pin that
    the driver's separate ``except NOISE_ERRORS`` branch maps it
    to the same :class:`PeerLinkClientError` surface (with the
    distinguishing ``"Noise handshake failed"`` text in the
    message) so the WS-command layer keeps a single
    ``UNAVAILABLE`` mapping while logs preserve the underlying
    cause.

    A *too-short* msg2 lands as ``NoiseValueError("Invalid
    length of public_bytes")``; a *length-correct but
    cryptographically wrong* msg2 lands as a bare ``ValueError``
    from the X25519 shared-key step (caught by the broader
    transport-failure branch). Both surface as
    ``PeerLinkClientError``, but only the short-msg2 path
    exercises the dedicated ``NOISE_ERRORS`` clause.
    """

    async def _handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # Read msg1 to keep the WS upgrade clean, then write a
        # too-short payload that trips ``NoiseValueError`` on
        # the public-key parse before any crypto runs.
        await ws.receive_bytes()
        await ws.send_bytes(b"")
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(PEER_LINK_PATH, _handler)
    server = TestServer(app)
    await server.start_server()
    initiator_priv = secrets.token_bytes(32)
    try:
        with pytest.raises(PeerLinkClientError, match="Noise handshake failed"):
            await drive_initiator_round_trip(
                hostname="127.0.0.1",
                port=server.port,
                identity_priv=initiator_priv,
                intent=PeerLinkIntent.PREVIEW,
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


# ---------------------------------------------------------------------------
# request_pair — happy path + receiver-side response shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_pair_open_window_returns_pending(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """Request_pair against an open pairing window returns PENDING + lands a peer row."""
    server, controller, expected_pin, _ = receiver_server
    await controller.set_pairing_window(open=True, client="test-tab")
    initiator_priv = secrets.token_bytes(32)

    # ``dashboard_id`` is deliberately a 16-char base64url shape
    # rather than the 32-char production form. Either passes the
    # receiver's validator (``DASHBOARD_ID_PATTERN`` allows 1-64
    # base64url chars); shorter keeps the test readable.
    result = await request_pair(
        hostname="127.0.0.1",
        port=server.port,
        identity_priv=initiator_priv,
        label="green",
        dashboard_id="abcdef0123456789",
    )

    assert result.status is IntentResponse.PENDING
    assert result.pin_sha256 == expected_pin
    assert len(result.remote_static_pub) == 32
    # Receiver's StoredPeer table got a new PENDING row keyed on
    # the offloader's dashboard_id.
    peers = controller.peers_snapshot()
    assert len(peers) == 1
    assert peers[0].dashboard_id == "abcdef0123456789"
    assert peers[0].status is PeerStatus.PENDING


@pytest.mark.asyncio
async def test_request_pair_closed_window_returns_no_pairing_window(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """Request_pair when the receiver window is closed returns NO_PAIRING_WINDOW."""
    server, _, _, _ = receiver_server
    initiator_priv = secrets.token_bytes(32)

    result = await request_pair(
        hostname="127.0.0.1",
        port=server.port,
        identity_priv=initiator_priv,
        label="green",
        dashboard_id="abcdef0123456789",
    )

    assert result.status is IntentResponse.NO_PAIRING_WINDOW


@pytest.mark.asyncio
async def test_request_pair_unknown_intent_response_raises_client_error() -> None:
    """A wire ``intent_response`` outside the known enum surfaces as PeerLinkClientError.

    Defends against a future receiver protocol bump that adds a
    new response code the offloader's enum doesn't know about.
    Custom WS handler completes the Noise XX handshake then
    sends ``{"intent_response": "weather"}`` — parses as JSON,
    isn't a valid :class:`IntentResponse` member.
    """
    receiver_priv = secrets.token_bytes(32)

    async def _handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sess = PeerLinkNoiseSession.responder(receiver_priv)
        sess.read_handshake_message(await ws.receive_bytes())
        await ws.send_bytes(sess.write_handshake_message(b""))
        sess.read_handshake_message(await ws.receive_bytes())
        await ws.send_bytes(sess.encrypt(b'{"intent_response": "weather"}'))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(PEER_LINK_PATH, _handler)
    server = TestServer(app)
    await server.start_server()
    initiator_priv = secrets.token_bytes(32)
    try:
        with pytest.raises(PeerLinkClientError, match="unknown intent_response"):
            await request_pair(
                hostname="127.0.0.1",
                port=server.port,
                identity_priv=initiator_priv,
                label="green",
                dashboard_id="abcdef0123456789",
            )
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# RemoteBuildController.request_pair — end-to-end through the WS-command shell
# ---------------------------------------------------------------------------


@pytest.fixture
def offloader_controller_dir(tmp_path: Path) -> Path:
    """Sibling directory to the receiver fixture's ``tmp_path``.

    Each side has its own peer-link key + dashboard cert, so the
    offloader's identities don't collide with the receiver's
    when both run in the same test process.
    """
    offloader_dir = tmp_path / "offloader"
    offloader_dir.mkdir()
    return offloader_dir


def _make_offloader_controller(*, config_dir: Path) -> RemoteBuildController:
    db = MagicMock()
    db.devices = MagicMock()
    db.devices.zeroconf = None
    db._dashboard_advertiser = None
    db.settings = MagicMock()
    db.settings.config_dir = config_dir
    return RemoteBuildController(db)


async def _saved_pairings(offloader: RemoteBuildController) -> list[StoredPairing]:
    """Flush any debounced save + return the on-disk pairings list.

    Empty list when the file doesn't exist (no save was ever
    scheduled, or the latest save wrote a file with no APPROVED
    rows). Walks ``_shutdown_callbacks`` in registration order —
    same hook ``DeviceBuilder.stop()`` walks in production — so
    pending debounced writes hit disk before we read.
    """
    for cb in offloader._shutdown_callbacks:
        await cb()
    saved = await offloader._pairings_store.async_load()
    return list(saved.pairings) if saved is not None else []


@pytest.mark.asyncio
async def test_controller_preview_pair_returns_receiver_pin(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    offloader_controller_dir: Path,
) -> None:
    """End-to-end: ``RemoteBuildController.preview_pair`` returns the receiver's pin."""
    server, _, expected_pin, _ = receiver_server

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    result = await offloader.preview_pair(
        hostname="127.0.0.1",
        port=server.port,
    )

    assert result == {"pin_sha256": expected_pin}
    # Preview is read-only on the offloader side too: no StoredPairing
    # row written until the user OOB-confirms and calls request_pair.
    assert await _saved_pairings(offloader) == []


@pytest.mark.asyncio
async def test_controller_preview_pair_unavailable_on_unreachable_receiver(
    offloader_controller_dir: Path,
    unused_tcp_port: int,
) -> None:
    """Receiver unreachable → CommandError(UNAVAILABLE)."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await offloader.preview_pair(
            hostname="127.0.0.1",
            port=unused_tcp_port,
        )
    assert exc.value.code == ErrorCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_controller_request_pair_persists_pending_row(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    offloader_controller_dir: Path,
) -> None:
    """End-to-end: ``RemoteBuildController.request_pair`` persists a PENDING StoredPairing."""
    server, receiver_controller, expected_pin, _ = receiver_server
    await receiver_controller.set_pairing_window(open=True, client="test-tab")

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    summary = await offloader.request_pair(
        hostname="127.0.0.1",
        port=server.port,
        pin_sha256=expected_pin,
        receiver_label="my-receiver",
        offloader_label="my-builder",
    )

    assert summary.receiver_hostname == "127.0.0.1"
    assert summary.receiver_port == server.port
    assert summary.pin_sha256 == expected_pin
    assert summary.label == "my-receiver"
    assert summary.status is PeerStatus.PENDING
    # PENDING entries live in the offloader controller's in-memory
    # dict; the persisted file stays APPROVED-only so a malicious
    # receiver can't bloat the offloader's settings file.
    assert await _saved_pairings(offloader) == []
    assert expected_pin in offloader._pairings


@pytest.mark.asyncio
async def test_controller_request_pair_pin_mismatch_raises_precondition_failed(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    offloader_controller_dir: Path,
) -> None:
    """User-supplied pin doesn't match the handshake → PRECONDITION_FAILED.

    A receiver-side identity rotation between preview and request would land here
    in production; the test forces the same shape by passing a wrong pin.
    """
    server, receiver_controller, _, _ = receiver_server
    await receiver_controller.set_pairing_window(open=True, client="test-tab")

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await offloader.request_pair(
            hostname="127.0.0.1",
            port=server.port,
            pin_sha256="b" * 64,  # not the receiver's actual pin
            receiver_label="my-receiver",
            offloader_label="my-builder",
        )
    assert exc.value.code == ErrorCode.PRECONDITION_FAILED
    # Pin-mismatch bails before persisting; offloader sidecar stays empty.
    assert await _saved_pairings(offloader) == []


@pytest.mark.asyncio
async def test_controller_request_pair_closed_window_raises_no_pairing_window(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    offloader_controller_dir: Path,
) -> None:
    """Receiver window closed → CommandError(NO_PAIRING_WINDOW)."""
    server, _, expected_pin, _ = receiver_server
    # Don't open the pairing window; receiver replies no_pairing_window.

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await offloader.request_pair(
            hostname="127.0.0.1",
            port=server.port,
            pin_sha256=expected_pin,
            receiver_label="my-receiver",
            offloader_label="my-builder",
        )
    assert exc.value.code == ErrorCode.NO_PAIRING_WINDOW


@pytest.mark.asyncio
async def test_controller_request_pair_unavailable_on_unreachable_receiver(
    offloader_controller_dir: Path,
    unused_tcp_port: int,
) -> None:
    """Receiver unreachable → CommandError(UNAVAILABLE)."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await offloader.request_pair(
            hostname="127.0.0.1",
            port=unused_tcp_port,
            pin_sha256="a" * 64,
            receiver_label="my-receiver",
            offloader_label="my-builder",
        )
    assert exc.value.code == ErrorCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_controller_request_pair_unexpected_status_raises_internal_error(
    offloader_controller_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Helper returns a known IntentResponse the controller doesn't expect → INTERNAL_ERROR.

    ``IntentResponse.OK`` is valid wire but only used by
    ``intent="preview"``; if a future receiver bug routed it
    back as a pair_request response it would slip past
    ``_intent_response_to_command_error`` (which only handles
    ``REJECTED`` / ``NO_PAIRING_WINDOW``) and hit the
    catch-all branch. Pin that branch with a mock so the
    contract holds: unexpected-but-valid wire values map to
    ``INTERNAL_ERROR``, not silently land as a corrupt local
    StoredPairing row.
    """
    fake_pin = "a" * 64

    async def _fake_request_pair(**_: object) -> RequestPairResult:
        return RequestPairResult(
            status=IntentResponse.OK,  # not in PENDING/APPROVED accept-set
            pin_sha256=fake_pin,
            remote_static_pub=b"\x00" * 32,
        )

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_request_pair",
        _fake_request_pair,
    )

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc:
        await offloader.request_pair(
            hostname="127.0.0.1",
            port=6055,
            pin_sha256=fake_pin,
            receiver_label="my-receiver",
            offloader_label="my-builder",
        )
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# RemoteBuildController offloader-side surface — unpair / list_pairings /
# _apply_pair_status_result branches (post-pivot to in-memory PENDING).
# The peer-link wire path is exercised by the request_pair end-to-end
# tests above; these focus on the dict / disk lifecycle without driving
# the wire. Live updates flow through the global ``subscribe_events``
# stream as ``OFFLOADER_PAIR_STATUS_CHANGED`` events; no separate
# ``subscribe_pairings`` channel is needed.
# ---------------------------------------------------------------------------


def _stub_pairing(
    *,
    receiver_hostname: str = "build.local",
    receiver_port: int = 6055,
    label: str = "desktop",
    paired_at: float = 1.0,
    pin_sha256: str | None = None,
    static_x25519_pub: bytes | None = None,
    status: PeerStatus = PeerStatus.PENDING,
) -> StoredPairing:
    """Build a :class:`StoredPairing` with sensible defaults for tests.

    Defaults to PENDING — most listener / pair-status tests
    operate on PENDING rows. Tests covering the persisted side opt
    into ``status=PeerStatus.APPROVED`` explicitly.
    """
    pub = static_x25519_pub if static_x25519_pub is not None else b"\x00" * 32
    pin = pin_sha256 if pin_sha256 is not None else "a" * 64
    return StoredPairing(
        receiver_hostname=receiver_hostname,
        receiver_port=receiver_port,
        pin_sha256=pin,
        static_x25519_pub=pub,
        label=label,
        paired_at=paired_at,
        status=status,
    )


@pytest.mark.asyncio
async def test_unpair_drops_pending_dict_entry_and_cancels_listener(
    offloader_controller_dir: Path,
) -> None:
    """Unpair on a PENDING row pops the dict + cancels its listener task."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055)
    offloader._pairings["a" * 64] = pairing
    # A no-op listener task standing in for the real long-poll
    # task; the test verifies cancel + cleanup, not the wire flow.
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    listener = asyncio.create_task(_park())
    offloader._pair_status_listeners["a" * 64] = listener
    # Settle one event-loop tick so the create_task is actually
    # scheduled before unpair tries to cancel it.
    await asyncio.sleep(0)

    result = await offloader.unpair(pin_sha256="a" * 64)

    assert result == {"removed": True}
    assert "a" * 64 not in offloader._pairings
    # ``unpair`` popped the listener entry from the registry —
    # the row is gone from the pin-keyed dict (4a-o part 6).
    assert "a" * 64 not in offloader._pair_status_listeners
    # The captured task reference was cancelled by ``unpair``'s
    # ``_cancel_pair_status_listener``; drain via ``gather`` so
    # the cancellation propagates without surfacing as a test
    # failure (same shape ``RemoteBuildController.stop()`` uses
    # for its own cancel-and-drain).
    await asyncio.gather(listener, return_exceptions=True)
    assert listener.cancelled()


@pytest.mark.asyncio
async def test_unpair_drops_persisted_row(
    offloader_controller_dir: Path,
) -> None:
    """Unpair on an APPROVED (persisted) row drops the disk entry.

    The unified ``_pairings`` dict carries both PENDING and
    APPROVED rows; ``unpair`` pops + schedules the debounced save.
    The test awaits the registered shutdown callback to flush the
    pending write before reading the on-disk file back — same hook
    ``DeviceBuilder.stop()`` walks in production.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local", receiver_port=6055, status=PeerStatus.APPROVED
    )
    # Land the row in RAM + force-flush an initial save so the
    # unpair has something to drop on disk too.
    offloader._pairings["a" * 64] = pairing
    offloader._pairings_store.async_delay_save(offloader._serialize_pairings, delay=0.0)
    for cb in offloader._shutdown_callbacks:
        await cb()

    result = await offloader.unpair(pin_sha256="a" * 64)

    assert result == {"removed": True}
    # Flush the second debounced save (the unpair's removal).
    for cb in offloader._shutdown_callbacks:
        await cb()
    saved = await offloader._pairings_store.async_load()
    assert saved is not None
    assert saved.pairings == []


@pytest.mark.asyncio
async def test_unpair_unknown_returns_removed_false_idempotent(
    offloader_controller_dir: Path,
) -> None:
    """Unpair on a non-existent (host, port) is a clean no-op."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    result = await offloader.unpair(pin_sha256="a" * 64)

    assert result == {"removed": False}


@pytest.mark.asyncio
async def test_pairings_snapshot_returns_ram_dict(
    offloader_controller_dir: Path,
) -> None:
    """pairings_snapshot is a sync read of the in-RAM ``_pairings`` dict.

    Unified dict carries both PENDING and APPROVED rows; the
    snapshot returns each row's ``status`` straight off the
    ``StoredPairing``. No executor hop, no disk read, no race
    against concurrent mutation.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pending = _stub_pairing(
        receiver_hostname="pending.local",
        receiver_port=6055,
        pin_sha256="a" * 64,
        label="pending-receiver",
        status=PeerStatus.PENDING,
    )
    approved = _stub_pairing(
        receiver_hostname="approved.local",
        receiver_port=6055,
        pin_sha256="b" * 64,
        label="approved-receiver",
        status=PeerStatus.APPROVED,
    )
    offloader._pairings["a" * 64] = pending
    offloader._pairings["b" * 64] = approved

    rows = offloader.pairings_snapshot()

    by_host = {row.receiver_hostname: row for row in rows}
    assert by_host["pending.local"].status is PeerStatus.PENDING
    assert by_host["approved.local"].status is PeerStatus.APPROVED


@pytest.mark.asyncio
async def test_pairings_snapshot_marks_open_link_as_connected(
    offloader_controller_dir: Path,
) -> None:
    """An APPROVED row whose ``pin_sha256`` is in ``_open_peer_links`` reports ``connected=True``.

    Mirror of the receiver-side
    ``test_peers_snapshot_marks_approved_with_active_session_connected``;
    pin the snapshot semantic so a future refactor that splits
    the open-link tracking from the snapshot read can't
    silently drop the membership check.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pin_a = "a" * 64
    pin_b = "b" * 64
    a = _stub_pairing(
        receiver_hostname="a.local",
        receiver_port=6055,
        label="a",
        pin_sha256=pin_a,
        status=PeerStatus.APPROVED,
    )
    b = _stub_pairing(
        receiver_hostname="b.local",
        receiver_port=6055,
        label="b",
        pin_sha256=pin_b,
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[pin_a] = a
    offloader._pairings[pin_b] = b
    # Only ``a`` has an open peer-link session.
    offloader._open_peer_links.add(pin_a)

    rows = {row.receiver_hostname: row for row in offloader.pairings_snapshot()}

    assert rows["a.local"].connected is True
    assert rows["b.local"].connected is False


@pytest.mark.asyncio
async def test_offloader_peer_link_event_listeners_update_open_set(
    offloader_controller_dir: Path,
) -> None:
    """OPENED adds + CLOSED discards from ``_open_peer_links``.

    The two callbacks are wired on real bus subscriptions in
    :meth:`start`, but the listeners themselves are sync and
    can be exercised directly with synthesised ``Event``
    payloads — that keeps the test focused on the set-mutation
    contract without standing up the full controller startup.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    pin = "a" * 64

    opened: OffloaderPeerLinkOpenedData = {
        "receiver_hostname": "host.local",
        "receiver_port": 6055,
        "pin_sha256": pin,
    }
    offloader._on_offloader_peer_link_opened(MagicMock(data=opened))
    assert pin in offloader._open_peer_links

    closed: OffloaderPeerLinkClosedData = {
        "receiver_hostname": "host.local",
        "receiver_port": 6055,
        "pin_sha256": pin,
        "reason": "peer_hung_up",
    }
    offloader._on_offloader_peer_link_closed(MagicMock(data=closed))
    assert pin not in offloader._open_peer_links

    # ``discard`` semantics: a CLOSED for a key we never saw
    # OPENED is a no-op rather than raising. Covers the
    # cold-start race where a stale CLOSED arrives before the
    # listener is ready.
    offloader._on_offloader_peer_link_closed(MagicMock(data=closed))
    assert pin not in offloader._open_peer_links


@pytest.mark.asyncio
async def test_start_seeds_pairings_dict_from_disk(
    offloader_controller_dir: Path,
) -> None:
    """``start()`` loads APPROVED pairings off disk into ``_pairings``.

    Pre-seed the per-file ``Store`` with a row, instantiate a
    fresh controller pointing at the same dir, call ``start()``,
    and assert the dict is populated. Pins the cold-start
    contract — APPROVED rows survive a controller restart so the
    user doesn't have to re-pair on every dashboard bounce.
    """
    # Stand up an offloader, write a row through its store, flush.
    seeder = _make_offloader_controller(config_dir=offloader_controller_dir)
    seeder._db.bus = MagicMock()
    pubkey = b"\xee" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    seeded = _stub_pairing(
        receiver_hostname="seeded.local",
        receiver_port=6055,
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        status=PeerStatus.APPROVED,
    )
    seeder._pairings[pin] = seeded
    seeder._pairings_store.async_delay_save(seeder._serialize_pairings, delay=0.0)
    for cb in seeder._shutdown_callbacks:
        await cb()

    # Fresh controller against the same config dir; ``start`` should
    # populate ``_pairings`` from disk. ``_db.devices`` returning
    # ``None`` short-circuits the rest of ``start`` (browser
    # construction etc.) — the pairings load runs unconditionally
    # ahead of the zeroconf-dependent block, so it lands either
    # way.
    fresh = _make_offloader_controller(config_dir=offloader_controller_dir)
    fresh._db.bus = MagicMock()
    fresh._db.devices = None  # short-circuit the post-load branches.
    await fresh.start()

    assert pin in fresh._pairings
    loaded = fresh._pairings[pin]
    assert loaded.pin_sha256 == pin
    assert loaded.static_x25519_pub == pubkey
    assert loaded.status is PeerStatus.APPROVED


# ---------------------------------------------------------------------------
# _apply_pair_status_result branches — dict mutation + event firing without
# running a real listener task.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_pair_status_result_approved_promotes_and_fires(
    offloader_controller_dir: Path,
) -> None:
    """APPROVED + matching pin → flip row status to APPROVED, schedule save, fire."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pubkey = b"\xaa" * 32
    pin = "a" * 64
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        status=PeerStatus.PENDING,
    )
    offloader._pairings["a" * 64] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.APPROVED, pin_sha256=pin
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    assert terminal is True
    # Row stays in the dict with promoted status — the unified
    # ``_pairings`` map carries both PENDING and APPROVED.
    assert offloader._pairings["a" * 64].status is PeerStatus.APPROVED
    saved = await _saved_pairings(offloader)
    assert len(saved) == 1
    assert saved[0].pin_sha256 == pin
    fire = offloader._db.bus.fire
    fire.assert_called_once()
    _, payload = fire.call_args.args
    assert payload["status"] == "approved"


@pytest.mark.asyncio
async def test_apply_pair_status_result_approved_pin_drift_drops_and_fires_removed(
    offloader_controller_dir: Path,
) -> None:
    """APPROVED + drifted pin → pop dict, fire pin_mismatch + removed events."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        pin_sha256="a" * 64,
        label="lab-pc",
    )
    offloader._pairings["a" * 64] = pairing
    # Receiver returned APPROVED but its pubkey hash doesn't match
    # what we stored; treat as peer-revoked rather than silently
    # adopting the new identity.
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.APPROVED, pin_sha256="b" * 64
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    assert terminal is True
    assert "a" * 64 not in offloader._pairings
    assert await _saved_pairings(offloader) == []
    # Two events should have fired in order: the discriminator
    # (PIN_MISMATCH) carrying the diagnostic detail + label,
    # then the existing STATUS_CHANGED("removed") that subscribers
    # use to drop the row from their pairings list. Pin order
    # (mismatch first) so subscribers see the diagnostic before
    # the row drop.
    fire_calls = offloader._db.bus.fire.call_args_list
    assert len(fire_calls) == 2
    first_event_type, first_payload = fire_calls[0].args
    assert first_event_type is EventType.OFFLOADER_PAIR_PIN_MISMATCH
    assert first_payload == {
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
        "receiver_label": "lab-pc",
        "pin_sha256": "a" * 64,
        "expected_pin": "a" * 64,
        "observed_pin": "b" * 64,
    }
    second_event_type, second_payload = fire_calls[1].args
    assert second_event_type is EventType.OFFLOADER_PAIR_STATUS_CHANGED
    assert second_payload["status"] == "removed"


@pytest.mark.asyncio
async def test_apply_pair_status_result_rejected_drops_and_fires_removed(
    offloader_controller_dir: Path,
) -> None:
    """REJECTED → pop dict, fire peer_revoked + removed events, terminal."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        label="lab-pc",
    )
    offloader._pairings["a" * 64] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.REJECTED, pin_sha256="a" * 64
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    assert terminal is True
    assert "a" * 64 not in offloader._pairings
    # Same fire-discriminator-first ordering as the pin-drift
    # branch: PEER_REVOKED carrying the row label, then
    # STATUS_CHANGED("removed") that drops the row.
    fire_calls = offloader._db.bus.fire.call_args_list
    assert len(fire_calls) == 2
    first_event_type, first_payload = fire_calls[0].args
    assert first_event_type is EventType.OFFLOADER_PAIR_PEER_REVOKED
    assert first_payload == {
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
        "receiver_label": "lab-pc",
        "pin_sha256": "a" * 64,
    }
    second_event_type, second_payload = fire_calls[1].args
    assert second_event_type is EventType.OFFLOADER_PAIR_STATUS_CHANGED
    assert second_payload["status"] == "removed"


@pytest.mark.asyncio
async def test_apply_pair_status_result_pin_drift_seeds_offloader_alert(
    offloader_controller_dir: Path,
) -> None:
    """Pin drift on APPROVED → ``_offloader_alerts`` gets a ``pin_mismatch`` row.

    The alert sits in RAM keyed on ``(hostname, port)`` so a
    late-subscriber picks it up via the
    ``initial_state.offloader_alerts`` snapshot push. Captures
    every field the wire shape carries — drift here would mean
    the snapshot's frontend rendering can't tell which row the
    alert is about.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        pin_sha256="a" * 64,
        label="lab-pc",
    )
    offloader._pairings["a" * 64] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.APPROVED, pin_sha256="b" * 64
    )

    await offloader._apply_pair_status_result(pairing, result)

    snapshot = offloader.offloader_alerts_snapshot()
    assert len(snapshot) == 1
    alert = snapshot[0]
    assert alert["kind"] == "pin_mismatch"
    assert alert["receiver_hostname"] == "rcv.local"
    assert alert["receiver_port"] == 6055
    assert alert["receiver_label"] == "lab-pc"
    assert alert["expected_pin"] == "a" * 64
    assert alert["observed_pin"] == "b" * 64
    assert alert["fired_at"] > 0


@pytest.mark.asyncio
async def test_apply_pair_status_result_rejected_seeds_offloader_alert(
    offloader_controller_dir: Path,
) -> None:
    """REJECTED → ``_offloader_alerts`` gets a ``peer_revoked`` row.

    Same RAM-snapshot pattern as the pin_mismatch path; the row
    carries the operator-facing label (the receiver coordinates
    aren't enough on their own — the pairings list has dropped
    the row by the time the frontend renders the alert).
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        label="lab-pc",
    )
    offloader._pairings["a" * 64] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.REJECTED, pin_sha256="a" * 64
    )

    await offloader._apply_pair_status_result(pairing, result)

    snapshot = offloader.offloader_alerts_snapshot()
    assert len(snapshot) == 1
    alert = snapshot[0]
    assert alert["kind"] == "peer_revoked"
    assert alert["receiver_hostname"] == "rcv.local"
    assert alert["receiver_port"] == 6055
    assert alert["receiver_label"] == "lab-pc"
    assert alert["fired_at"] > 0


@pytest.mark.asyncio
async def test_unpair_clears_offloader_alert_and_fires_dismissed(
    offloader_controller_dir: Path,
) -> None:
    """``unpair`` drops the pairing AND auto-clears any pending alert."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055)
    offloader._pairings["a" * 64] = pairing
    offloader._offloader_alerts["a" * 64] = {
        "kind": "peer_revoked",
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
        "receiver_label": "lab-pc",
        "fired_at": 1.0,
    }

    result = await offloader.unpair(pin_sha256="a" * 64)

    assert result == {"removed": True}
    assert offloader.offloader_alerts_snapshot() == []
    fire_event_types = [call.args[0] for call in offloader._db.bus.fire.call_args_list]
    # ``unpair`` fires status_changed(removed) for the pairing
    # row and alert_dismissed for the alert; both are needed for
    # cross-tab sync of the two distinct lists.
    assert EventType.OFFLOADER_PAIR_STATUS_CHANGED in fire_event_types
    assert EventType.OFFLOADER_PAIR_ALERT_DISMISSED in fire_event_types


@pytest.mark.asyncio
async def test_apply_pair_status_result_unexpected_status_logs_and_continues(
    offloader_controller_dir: Path,
) -> None:
    """Unexpected receiver response (PENDING/OK/NO_PAIRING_WINDOW) returns False (re-loop)."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055)
    offloader._pairings["a" * 64] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.OK, pin_sha256="a" * 64
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    # Not terminal — the listener loop reconnects after a backoff.
    assert terminal is False
    # Pending entry untouched.
    assert "a" * 64 in offloader._pairings
    offloader._db.bus.fire.assert_not_called()


# ---------------------------------------------------------------------------
# Race tests — listener-vs-unpair, listener-cancel-before-disk, re-pair
# listener replacement, dict invariants under concurrent mutation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_pair_status_result_approved_after_unpair_does_not_resurrect_row(
    offloader_controller_dir: Path,
) -> None:
    """APPROVED branch must not write the row to disk if the user already unpaired.

    Race shape: listener is parked on
    ``await await_pair_status(...)``. User clicks Unpair.
    ``unpair`` pops the dict entry + cancels the listener, but
    cancellation only takes effect at the next await checkpoint
    — if the receiver response had already arrived, the listener's
    ``_apply_pair_status_result`` runs with the captured pairing
    from its closure. The APPROVED branch's
    ``self._pairings.pop(key, None)`` returns None (no
    dict entry left), the listener bails terminal without
    persisting.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pin = "a" * 64
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055, pin_sha256=pin)
    # Simulate the unpair path: dict entry was already popped.
    # The pairing in the listener's closure is what remains
    # (captured at spawn time).
    assert "a" * 64 not in offloader._pairings

    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.APPROVED, pin_sha256=pin
    )
    terminal = await offloader._apply_pair_status_result(pairing, result)

    # Terminal exit, no persist, no event fire.
    assert terminal is True
    assert await _saved_pairings(offloader) == []
    offloader._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_unpair_cancels_listener_before_disk_transaction(
    offloader_controller_dir: Path,
) -> None:
    """``unpair`` cancels the pair-status listener before awaiting the disk write.

    A slow disk transaction must not delay the WS-close to the
    receiver. By the time ``await loop.run_in_executor`` returns,
    the listener is already cancelled.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    key = "a" * 64
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055)
    offloader._pairings[key] = pairing
    listener = asyncio.create_task(_park())
    offloader._pair_status_listeners[key] = listener
    await asyncio.sleep(0)

    await offloader.unpair(pin_sha256="a" * 64)

    with pytest.raises(asyncio.CancelledError):
        await listener
    assert key not in offloader._pairings
    assert key not in offloader._pair_status_listeners


@pytest.mark.asyncio
async def test_unpair_fires_offloader_pair_status_changed_removed(
    offloader_controller_dir: Path,
) -> None:
    """Unpair fires OFFLOADER_PAIR_STATUS_CHANGED("removed") on the local bus.

    Mirrors how the receiver-side ``remove_peer`` fires
    ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` — other clients on the
    global ``subscribe_events`` stream see the removal without
    re-fetching the pairings snapshot.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055)
    offloader._pairings["a" * 64] = pairing

    result = await offloader.unpair(pin_sha256="a" * 64)

    assert result == {"removed": True}
    fire = offloader._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.OFFLOADER_PAIR_STATUS_CHANGED
    assert payload == {
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "status": "removed",
    }


@pytest.mark.asyncio
async def test_request_pair_clears_offloader_alert_for_same_receiver(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful re-pair against the same ``(host, port)`` clears any pending alert.

    Pin mismatch / peer revoked alerts are RAM-only signals
    about a transient detection. Once the user successfully
    re-pairs, the alert is stale; auto-resolving it on the
    re-pair path keeps the operator from having to dismiss
    twice (once on the request, once on the alert UI).
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pubkey = b"\x33" * 32
    pin = hashlib.sha256(pubkey).hexdigest()

    async def _fake_request_pair(**_: object) -> RequestPairResult:
        return RequestPairResult(
            status=IntentResponse.PENDING,
            pin_sha256=pin,
            remote_static_pub=pubkey,
        )

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_request_pair",
        _fake_request_pair,
    )
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "dashboard-stub"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller._load_offloader_identities",
        lambda _config_dir: (fake_identity, fake_dashboard),
    )
    # Park the spawned listener on an unfulfilled wait so the
    # test exits cleanly.
    park = asyncio.Event()

    async def _fake_await_pair_status(
        **_: object,
    ) -> remote_build_peer_link_client.PairStatusResult:
        await park.wait()
        raise AssertionError("park event should never be set in this test")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_await_pair_status",
        _fake_await_pair_status,
    )

    # Pre-seed an alert as if a prior pair-status detection had
    # registered one against this receiver.
    offloader._offloader_alerts["a" * 64] = {
        "kind": "pin_mismatch",
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "receiver_label": "lab-pc",
        "expected_pin": "a" * 64,
        "observed_pin": "b" * 64,
        "fired_at": 1.0,
    }

    await offloader.request_pair(
        hostname="rcv.local",
        port=6055,
        pin_sha256=pin,
        receiver_label="lab-pc",
        offloader_label="off",
    )

    # Auto-resolve fired the dismissed event so cross-tab clients
    # drop the row from their alerts list, and the snapshot is
    # empty for fresh subscribers.
    assert offloader.offloader_alerts_snapshot() == []
    fire_event_types = [call.args[0] for call in offloader._db.bus.fire.call_args_list]
    assert EventType.OFFLOADER_PAIR_ALERT_DISMISSED in fire_event_types

    # Cleanup: cancel the listener so the test exits clean.
    listener = offloader._pair_status_listeners.get(pin)
    if listener is not None:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


@pytest.mark.asyncio
async def test_unpair_does_not_fire_event_when_nothing_to_remove(
    offloader_controller_dir: Path,
) -> None:
    """Idempotent ``unpair`` on an unknown (host, port) returns removed=False.

    No spurious event fires on the no-op path.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    result = await offloader.unpair(pin_sha256="a" * 64)

    assert result == {"removed": False}
    offloader._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_request_pair_repair_then_unpair_clean_state(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair → re-pair → unpair leaves no dangling listener / dict entry.

    Each ``request_pair`` cancels the prior listener for the same
    (host, port) and spawns a fresh one with the new pairing in
    its closure; ``unpair`` then cancels the latest listener and
    drops the dict entry.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pubkey1 = b"\x11" * 32
    pin1 = hashlib.sha256(pubkey1).hexdigest()
    pubkey2 = b"\x22" * 32
    pin2 = hashlib.sha256(pubkey2).hexdigest()

    fake_results = [
        RequestPairResult(
            status=IntentResponse.PENDING, pin_sha256=pin1, remote_static_pub=pubkey1
        ),
        RequestPairResult(
            status=IntentResponse.PENDING, pin_sha256=pin2, remote_static_pub=pubkey2
        ),
    ]

    async def _fake_request_pair(**_: object) -> RequestPairResult:
        return fake_results.pop(0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_request_pair",
        _fake_request_pair,
    )

    # The spawned ``_await_pair_status_flip`` listener would
    # otherwise call the real ``peer_link_await_pair_status`` +
    # ``_load_offloader_identities`` — hitting real DNS for
    # ``rcv.local``. Park each listener on an ``asyncio.Event``
    # via the fake instead, and signal back when each listener
    # has actually reached the parked state — the test below
    # uses that signal to force the precise race that previously
    # orphaned ``listener_v2``.
    park = asyncio.Event()
    parked_signals: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    park_call_index = 0

    async def _fake_await_pair_status(
        **_: object,
    ) -> remote_build_peer_link_client.PairStatusResult:
        nonlocal park_call_index
        if park_call_index < len(parked_signals):
            parked_signals[park_call_index].set()
        park_call_index += 1
        await park.wait()
        raise AssertionError("park event should never be set in this test")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_await_pair_status",
        _fake_await_pair_status,
    )
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "dashboard-stub"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller._load_offloader_identities",
        lambda _config_dir: (fake_identity, fake_dashboard),
    )

    # First pair lands PENDING with pin1.
    await offloader.request_pair(
        hostname="rcv.local",
        port=6055,
        pin_sha256=pin1,
        receiver_label="rcv-1",
        offloader_label="off",
    )
    listener_v1 = offloader._pair_status_listeners[pin1]

    # Wait until ``listener_v1`` has actually reached its
    # ``await peer_link_await_pair_status`` parked state.
    # Without this barrier the second ``request_pair`` below
    # often cancels ``listener_v1`` while it's still at its
    # initial ``run_in_executor`` await — so the cancel raises
    # before the body's ``try`` block, ``listener_v1``'s
    # ``finally`` never runs, and the orphan-listener bug
    # (``listener_v1``'s ``finally`` evicting ``listener_v2``
    # from ``_pair_status_listeners``) is masked. This was the
    # exact CI/local divergence behind the original flake — the
    # bug only fires when ``listener_v1`` advances past the
    # critical section before being cancelled.
    await parked_signals[0].wait()

    # Re-pair with pin2 — listener_v1 must be cancelled, listener_v2 spawned.
    await offloader.request_pair(
        hostname="rcv.local",
        port=6055,
        pin_sha256=pin2,
        receiver_label="rcv-2",
        offloader_label="off",
    )
    listener_v2 = offloader._pair_status_listeners[pin2]
    assert listener_v2 is not listener_v1
    # Drain the cancelled v1 task — same cancel-and-gather
    # shape ``RemoteBuildController.stop()`` uses for its
    # task-set drain.
    await asyncio.gather(listener_v1, return_exceptions=True)
    assert listener_v1.cancelled()
    # Re-pair under a new pin landed a new entry under pin2;
    # the sweep dropped the old entry under pin1.
    assert offloader._pairings[pin2].pin_sha256 == pin2
    assert pin1 not in offloader._pairings

    # Unpair cancels listener_v2 + clears the dict.
    await offloader.unpair(pin_sha256=pin2)
    await asyncio.gather(listener_v2, return_exceptions=True)
    assert listener_v2.cancelled()
    assert offloader._pairings == {}
    assert offloader._pair_status_listeners == {}


@pytest.mark.asyncio
async def test_spawn_pair_status_listener_is_idempotent_on_running_task(
    offloader_controller_dir: Path,
) -> None:
    """A second spawn for the same key returns early; the running task isn't replaced."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    key = "a" * 64
    existing = asyncio.create_task(_park())
    offloader._pair_status_listeners[key] = existing
    await asyncio.sleep(0)  # schedule the task
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055)

    offloader._spawn_pair_status_listener(pairing)

    # The dict still points at the original task — the spawn was
    # a no-op because a listener was already running for this key.
    assert offloader._pair_status_listeners[key] is existing
    park.set()
    await existing


@pytest.mark.asyncio
async def test_cancel_pair_status_listener_noop_when_absent(
    offloader_controller_dir: Path,
) -> None:
    """Cancelling a non-existent listener is a no-op."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    # No-op; doesn't raise.
    offloader._cancel_pair_status_listener("a" * 64)


@pytest.mark.asyncio
async def test_request_pair_repair_against_pending_cancels_old_listener(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-pair against a row already in ``_pairings`` cancels the old listener.

    Without the cancel, the old listener task keeps running with
    a stale ``StoredPairing`` (old pubkey / pin captured in its
    closure). On a real receiver flip the old listener would
    falsely detect pin drift even though the user just
    re-confirmed the new pin.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    key_old = "a" * 64
    old_pairing = _stub_pairing(
        receiver_hostname="rcv.local", receiver_port=6055, pin_sha256="a" * 64
    )
    offloader._pairings[key_old] = old_pairing
    old_listener = asyncio.create_task(_park())
    offloader._pair_status_listeners[key_old] = old_listener
    await asyncio.sleep(0)

    # Stub the wire round-trip so request_pair completes without
    # hitting a real receiver.
    new_pin = "b" * 64
    new_pubkey = b"\x77" * 32

    async def _fake_request_pair(**_: object) -> RequestPairResult:
        return RequestPairResult(
            status=IntentResponse.PENDING,
            pin_sha256=new_pin,
            remote_static_pub=new_pubkey,
        )

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_request_pair",
        _fake_request_pair,
    )

    summary = await offloader.request_pair(
        hostname="rcv.local",
        port=6055,
        pin_sha256=new_pin,
        receiver_label="rcv-label",
        offloader_label="off-label",
    )

    # Old listener got cancelled (cleared pin-drift exposure);
    # drain via gather, same shape as production's
    # ``RemoteBuildController.stop()``.
    await asyncio.gather(old_listener, return_exceptions=True)
    assert old_listener.cancelled()
    # New listener spawned with the fresh pairing under the new pin key.
    new_listener = offloader._pair_status_listeners.get(new_pin)
    assert new_listener is not None
    assert new_listener is not old_listener
    new_listener.cancel()
    await asyncio.gather(new_listener, return_exceptions=True)
    assert new_listener.cancelled()
    # The dict entry now holds the new pin.
    assert offloader._pairings[new_pin].pin_sha256 == new_pin
    assert summary.pin_sha256 == new_pin
    # Old (pin-keyed) entry is gone — re-pair under a new pin
    # creates a fresh row, doesn't shadow the old one.
    assert key_old not in offloader._pairings


@pytest.mark.asyncio
async def test_request_pair_already_approved_persists_to_disk(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    offloader_controller_dir: Path,
) -> None:
    """Re-pair against an already-approved row persists APPROVED, no listener spawn.

    Drives the full e2e path: first request_pair lands PENDING,
    receiver-side admin Accepts (promoting to APPROVED), then a
    second request_pair finds the receiver returning
    ``intent_response=approved`` immediately and writes the row
    to the offloader's persistent file.
    """
    server, receiver_controller, expected_pin, _ = receiver_server
    await receiver_controller.set_pairing_window(open=True, client="receiver-tab")
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()

    # First pair: lands PENDING on the receiver.
    summary_pending = await offloader.request_pair(
        hostname="127.0.0.1",
        port=server.port,
        pin_sha256=expected_pin,
        receiver_label="rcv-label",
        offloader_label="off-label",
    )
    assert summary_pending.status is PeerStatus.PENDING

    # Cancel the offloader's listener so the second request_pair
    # below doesn't race with it; we're only testing the
    # already-approved → persist path.
    offloader._cancel_pair_status_listener(expected_pin)

    # Promote the receiver-side row to APPROVED so the next
    # request_pair gets the short-circuit path.
    [pending_peer] = receiver_controller._pending_peers.values()
    await receiver_controller.approve_peer(dashboard_id=pending_peer.dashboard_id)

    # Second pair: receiver returns APPROVED immediately; the
    # offloader writes the row to disk + drops any prior
    # PENDING dict entry.
    summary_approved = await offloader.request_pair(
        hostname="127.0.0.1",
        port=server.port,
        pin_sha256=expected_pin,
        receiver_label="rcv-label",
        offloader_label="off-label",
    )
    assert summary_approved.status is PeerStatus.APPROVED
    saved = await _saved_pairings(offloader)
    assert len(saved) == 1
    assert saved[0].pin_sha256 == expected_pin
    # The row stays in the unified dict with APPROVED status —
    # no separate PENDING entry, no double-counting on the wire.
    assert offloader._pairings[expected_pin].status is PeerStatus.APPROVED


@pytest.mark.asyncio
async def test_lookup_peer_for_status_pending_dict_pin_mismatch_returns_rejected(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """An offloader presenting a wrong pin against a PENDING dict entry → REJECTED.

    The dict-then-list lookup in ``_lookup_peer_response`` runs
    the pin check on the dict entry and returns REJECTED on
    mismatch (not PENDING) so a peer with a stale / impersonated
    pubkey can't pretend to be a legitimate pending offloader.
    """
    _, controller, _, _ = receiver_server
    pubkey = b"\x44" * 32
    real_pin = "a" * 64
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._pending_peers["alpha"] = StoredPeer(
        dashboard_id="alpha",
        pin_sha256=real_pin,
        static_x25519_pub=pubkey,
        label="alpha",
        paired_at=1.0,
    )

    response = await controller.lookup_peer_for_status(dashboard_id="alpha", pin_sha256="b" * 64)

    assert response is IntentResponse.REJECTED


# ---------------------------------------------------------------------------
# await_pair_status — exercise the wire helper directly (rather than only
# through the controller's listener task) so its module-level coverage
# tracks against the surface area it actually owns.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_pair_status_returns_approved_when_receiver_approved(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    offloader_controller_dir: Path,
) -> None:
    """await_pair_status against an APPROVED receiver row returns APPROVED + receiver pin."""
    server, controller, expected_pin, _ = receiver_server
    pubkey = b"\x55" * 32
    pair_pin = hashlib.sha256(pubkey).hexdigest()
    # Seed the receiver with an approved peer matching the
    # offloader's identity below.
    await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _seed_approved_peer_sync(controller, "tester", pair_pin, pubkey),
    )

    # Make the offloader's identity match the seeded pubkey so
    # the receiver's pin check passes.
    offloader_id_priv = secrets.token_bytes(32)
    # Tests don't bother with a real X25519 keypair — the
    # receiver's check is on the *handshake's* derived pubkey,
    # not the seeded value. Rather than mock that, re-derive
    # via :class:`X25519PrivateKey`.
    pubkey_real = (
        X25519PrivateKey.from_private_bytes(offloader_id_priv).public_key().public_bytes_raw()
    )
    real_pin = hashlib.sha256(pubkey_real).hexdigest()
    # Replace the seeded peer with the actually-derived one.
    await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: _seed_approved_peer_sync(controller, "tester", real_pin, pubkey_real),
    )

    result = await remote_build_peer_link_client.await_pair_status(
        hostname="127.0.0.1",
        port=server.port,
        identity_priv=offloader_id_priv,
        dashboard_id="tester",
    )

    assert result.status is IntentResponse.APPROVED
    assert result.pin_sha256 == expected_pin


@pytest.mark.asyncio
async def test_await_pair_status_unknown_dashboard_id_returns_rejected(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """await_pair_status against an unknown dashboard_id returns REJECTED."""
    server, _, _, _ = receiver_server
    offloader_id_priv = secrets.token_bytes(32)

    result = await remote_build_peer_link_client.await_pair_status(
        hostname="127.0.0.1",
        port=server.port,
        identity_priv=offloader_id_priv,
        dashboard_id="ghost",
    )

    assert result.status is IntentResponse.REJECTED


def _seed_approved_peer_sync(
    controller: RemoteBuildController,
    dashboard_id: str,
    pin: str,
    pubkey: bytes,
) -> None:
    """Sync helper: drop+rewrite the receiver's APPROVED peer dict."""
    controller._approved_peers[dashboard_id] = StoredPeer(
        dashboard_id=dashboard_id,
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        label=dashboard_id,
        paired_at=1.0,
    )


@pytest.mark.asyncio
async def test_await_pair_status_unknown_intent_response_raises_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``intent_response`` not in the IntentResponse enum becomes a PeerLinkClientError.

    A future receiver protocol bump that emits a new value on
    pair_status would otherwise pass through as a runtime
    ``ValueError`` from the enum constructor; the wire helper
    wraps it for the WS-command layer's UNAVAILABLE mapping.
    """

    async def _fake_round_trip(**_: object) -> object:
        return remote_build_peer_link_client.InitiatorRoundTrip(
            intent_response="unknown_future_value",
            remote_static_pub=b"\x00" * 32,
            response={"intent_response": "unknown_future_value"},
        )

    monkeypatch.setattr(
        remote_build_peer_link_client, "drive_initiator_round_trip", _fake_round_trip
    )

    with pytest.raises(PeerLinkClientError, match="unknown intent_response"):
        await remote_build_peer_link_client.await_pair_status(
            hostname="rcv.local",
            port=6055,
            identity_priv=b"\x00" * 32,
            dashboard_id="alpha",
        )


@pytest.mark.asyncio
async def test_stop_cancels_pair_status_listeners(
    offloader_controller_dir: Path,
) -> None:
    """stop() cancels + drains every running pair-status listener task."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    task_a = asyncio.create_task(_park())
    task_b = asyncio.create_task(_park())
    offloader._pair_status_listeners[("a.local", 6055)] = task_a
    offloader._pair_status_listeners[("b.local", 6055)] = task_b
    await asyncio.sleep(0)  # schedule both

    await offloader.stop()

    # Both tasks completed (cancelled) and the dict was cleared.
    assert task_a.done()
    assert task_b.done()
    assert offloader._pair_status_listeners == {}


@pytest.mark.asyncio
async def test_pair_status_listener_loop_backs_off_on_transport_error(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listener loop catches PeerLinkClientError, sleeps backoff, reconnects.

    Drives the listener body directly (cancel after the second
    poll attempt) so the backoff branch is covered without a
    real-time test wait. Patches ``_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS``
    to a tiny value so the test doesn't actually sleep 2s, and
    swaps in a fake ``peer_link_await_pair_status`` that raises
    on the first call and returns APPROVED on the second.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller._PAIR_STATUS_RECONNECT_BACKOFF_SECONDS",
        0.0,
    )
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pubkey = b"\x88" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        pin_sha256=pin,
        static_x25519_pub=pubkey,
    )
    offloader._pairings[pin] = pairing

    calls = 0

    async def _fake_poll(**_: object) -> remote_build_peer_link_client.PairStatusResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PeerLinkClientError("simulated transport blip")
        return remote_build_peer_link_client.PairStatusResult(
            status=IntentResponse.APPROVED, pin_sha256=pin
        )

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_await_pair_status",
        _fake_poll,
    )
    # Stub identity load so it doesn't try to read real key files.
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "alpha"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller._load_offloader_identities",
        lambda _config_dir: (fake_identity, fake_dashboard),
    )

    await offloader._await_pair_status_flip(pairing)

    # Two poll attempts (transport error → backoff → success).
    assert calls == 2
    # Terminal branch ran: row stays in the dict but is now
    # APPROVED, and an approved event fired.
    assert offloader._pairings[pin].status is PeerStatus.APPROVED


@pytest.mark.asyncio
async def test_pair_status_listener_loop_backs_off_on_unexpected_status(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listener loop catches non-terminal apply result + backs off before reconnecting.

    A receiver returning OK / PENDING / NO_PAIRING_WINDOW from a
    pair_status query is a protocol bug; the listener doesn't
    tight-loop against it.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller._PAIR_STATUS_RECONNECT_BACKOFF_SECONDS",
        0.0,
    )
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pubkey = b"\x99" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        pin_sha256=pin,
        static_x25519_pub=pubkey,
    )
    offloader._pairings[pin] = pairing

    calls = 0

    async def _fake_poll(**_: object) -> remote_build_peer_link_client.PairStatusResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Unexpected status — listener should sleep + reconnect.
            return remote_build_peer_link_client.PairStatusResult(
                status=IntentResponse.OK, pin_sha256=pin
            )
        return remote_build_peer_link_client.PairStatusResult(
            status=IntentResponse.REJECTED, pin_sha256=pin
        )

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller.peer_link_await_pair_status",
        _fake_poll,
    )
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "alpha"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.controller._load_offloader_identities",
        lambda _config_dir: (fake_identity, fake_dashboard),
    )

    await offloader._await_pair_status_flip(pairing)

    # Two poll attempts (OK → backoff → REJECTED → terminal).
    assert calls == 2
    assert pin not in offloader._pairings


# ---------------------------------------------------------------------------
# Phase 5a-2 — PeerLinkClient long-lived offloader-side session.
# ---------------------------------------------------------------------------


def _build_handshake_pair() -> tuple[PeerLinkNoiseSession, PeerLinkNoiseSession]:
    """Drive a 3-message Noise XX handshake against itself; return both sides finalised."""
    initiator = PeerLinkNoiseSession.initiator(secrets.token_bytes(32))
    responder = PeerLinkNoiseSession.responder(secrets.token_bytes(32))
    responder.read_handshake_message(initiator.write_handshake_message(b""))
    initiator.read_handshake_message(responder.write_handshake_message(b""))
    responder.read_handshake_message(initiator.write_handshake_message(b""))
    return initiator, responder


class _ParkingWs:
    """Async-iterable fake WS that parks ``__anext__`` until ``close()`` is called.

    Used in unit tests for :meth:`PeerLinkClient._run_session_loops`
    where we want the receive loop to park until something else
    (a stubbed heartbeat, a manual close call) wakes it. Once
    ``close()`` runs, the parked ``__anext__`` raises
    :class:`StopAsyncIteration` and the receive loop exits.
    """

    def __init__(self, closed_event: asyncio.Event) -> None:
        self._closed_event = closed_event
        self.closed = False

    async def send_bytes(self, _data: bytes) -> None:  # pragma: no cover — no-op
        pass

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()

    def __aiter__(self) -> _ParkingWs:
        return self

    async def __anext__(self) -> Any:
        await self._closed_event.wait()
        raise StopAsyncIteration


class _DeliverFramesWs(_ParkingWs):
    """Deliver each entry in *frames* on consecutive ``__anext__`` calls, then park.

    Every dispatcher test on the offloader-side receive loop
    needs the same shape: encrypt one (or a few) frame dicts
    with the receiver-side ``PeerLinkNoiseSession``, deliver
    them through an async WS iterator, then park until the
    test signals the close. Inlining a dedicated ``_XxxWs``
    subclass per test was ~12 lines x 6 tests of duplicate
    boilerplate; this helper takes a list of plaintext frame
    dicts and a ``responder`` session to encrypt them with.
    """

    def __init__(
        self,
        closed_event: asyncio.Event,
        responder: PeerLinkNoiseSession,
        frames: list[dict[str, Any]],
    ) -> None:
        super().__init__(closed_event)
        self._responder = responder
        self._frames = list(frames)
        self._index = 0

    async def __anext__(self) -> Any:
        if self._index < len(self._frames):
            frame = self._responder.encrypt(_json.dumps(self._frames[self._index]))
            self._index += 1
            return WSMessage(type=WSMsgType.BINARY, data=frame, extra="")
        await self._closed_event.wait()
        raise StopAsyncIteration


def _make_offloader_client(
    bus: EventBus | Any,
    *,
    receiver_hostname: str = "receiver.local",
    receiver_port: int = 6055,
    pinned_static_x25519_pub: bytes = b"\x00" * 32,
    pin_sha256: str = "a" * 64,
    receiver_label: str = "test-receiver",
    dashboard_id: str = "alpha",
) -> PeerLinkClient:
    """Build a :class:`PeerLinkClient` with the defaults every offloader test uses.

    Every constructor-arg has a default; tests that need a
    non-default ``receiver_hostname`` / ``pin_sha256`` /
    ``pinned_static_x25519_pub`` pass overrides. The 8-line
    construction was repeated ~30 times across the file before
    this helper.
    """
    return PeerLinkClient(
        receiver_hostname=receiver_hostname,
        receiver_port=receiver_port,
        identity_priv=secrets.token_bytes(32),
        dashboard_id=dashboard_id,
        pinned_static_x25519_pub=pinned_static_x25519_pub,
        pin_sha256=pin_sha256,
        receiver_label=receiver_label,
        bus=bus,
    )


@asynccontextmanager
async def _drive_session_with_frames(
    client: PeerLinkClient,
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, Any]],
) -> AsyncIterator[None]:
    """Park ``client._run_session_loops`` against synthetic *frames* for the with-block.

    Folds the receive-loop scaffolding every offloader-side
    dispatcher test repeats: Noise handshake pair, single-shot
    deliver-then-park WS, stubbed heartbeat, kicked-off
    :meth:`PeerLinkClient._run_session_loops` task. On context
    exit the close event fires and the drive task is awaited
    so test teardown deterministically unwinds.

    The client is constructed by the caller (via
    :func:`_make_offloader_client`) so test code can mutate it
    before the session opens — e.g., pre-register an entry on
    :attr:`PeerLinkClient._submit_job_acks` so an inbound
    ``submit_job_ack`` frame finds the matching future.
    """
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()
    ws = _DeliverFramesWs(closed_event, responder, frames)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _idle_heartbeat(**_kwargs: Any) -> None:
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _idle_heartbeat)

    drive_task = asyncio.create_task(client._run_session_loops(channel))
    try:
        yield
    finally:
        closed_event.set()
        await drive_task


async def _seed_approved_peer_for_initiator(
    receiver_controller: RemoteBuildController,
    *,
    dashboard_id: str,
    initiator_priv: bytes,
) -> None:
    """Seed an APPROVED ``StoredPeer`` on the receiver matching *initiator_priv*."""
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    receiver_controller._approved_peers[dashboard_id] = StoredPeer(
        dashboard_id=dashboard_id,
        pin_sha256=hashlib.sha256(initiator_pub).hexdigest(),
        static_x25519_pub=initiator_pub,
        label=dashboard_id,
        paired_at=1.0,
    )


@pytest.mark.asyncio
async def test_peer_link_client_fires_opened_after_handshake(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """A real PeerLinkClient against a real receiver fires OFFLOADER_PEER_LINK_OPENED."""
    server, receiver, _, receiver_pub = receiver_server
    initiator_priv = secrets.token_bytes(32)
    await _seed_approved_peer_for_initiator(
        receiver, dashboard_id="alpha", initiator_priv=initiator_priv
    )

    bus = EventBus()
    opened = capture_events(bus, EventType.OFFLOADER_PEER_LINK_OPENED)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=initiator_priv,
        dashboard_id="alpha",
        pinned_static_x25519_pub=receiver_pub,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(opened.received.wait(), timeout=2.0)
        assert len(opened) == 1
        assert opened[0]["receiver_hostname"] == "127.0.0.1"
        assert opened[0]["receiver_port"] == server.port
    finally:
        await cancel_and_drain(task)


@pytest.mark.asyncio
async def test_peer_link_client_fires_closed_on_cancel(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """Cancelling the client task fires OFFLOADER_PEER_LINK_CLOSED with client_stopped."""
    server, receiver, _, receiver_pub = receiver_server
    initiator_priv = secrets.token_bytes(32)
    await _seed_approved_peer_for_initiator(
        receiver, dashboard_id="alpha", initiator_priv=initiator_priv
    )

    bus = EventBus()
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)
    opened = asyncio.Event()
    bus.add_listener(EventType.OFFLOADER_PEER_LINK_OPENED, lambda e: opened.set())

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=initiator_priv,
        dashboard_id="alpha",
        pinned_static_x25519_pub=receiver_pub,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(opened.wait(), timeout=2.0)

    await cancel_and_drain(task)

    # CancelledError handler in run() fires CLOSED before
    # propagating. Reason is "client_stopped" because the
    # offloader-side initiated.
    assert len(closed) >= 1
    assert closed[-1]["reason"] == "client_stopped"


@pytest.mark.asyncio
async def test_peer_link_client_orphans_on_superseded(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """A receiver-side ``terminate{reason: superseded}`` orphans the client.

    Reconnecting after a superseded close would just collide
    with the offloader instance that took our slot. Drive this
    by running two clients with the same dashboard_id /
    initiator_priv against one receiver — the second connect
    kicks the first via ``terminate{reason: superseded}``, the
    first's run loop sees that and orphans rather than retrying.
    """
    server, receiver, _, receiver_pub = receiver_server
    initiator_priv = secrets.token_bytes(32)
    await _seed_approved_peer_for_initiator(
        receiver, dashboard_id="alpha", initiator_priv=initiator_priv
    )

    bus = EventBus()
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)
    opened1 = asyncio.Event()
    opened2 = asyncio.Event()

    def _on_open(e: Any) -> None:
        if not opened1.is_set():
            opened1.set()
        else:
            opened2.set()

    bus.add_listener(EventType.OFFLOADER_PEER_LINK_OPENED, _on_open)

    client1 = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=initiator_priv,
        dashboard_id="alpha",
        pinned_static_x25519_pub=receiver_pub,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task1 = asyncio.create_task(client1.run())
    await asyncio.wait_for(opened1.wait(), timeout=2.0)

    client2 = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=initiator_priv,
        dashboard_id="alpha",
        pinned_static_x25519_pub=receiver_pub,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task2 = asyncio.create_task(client2.run())
    try:
        await asyncio.wait_for(opened2.wait(), timeout=2.0)
        # Wait for client1 to observe the superseded close. ``run()``
        # exits cleanly (returns) on superseded — no exception to
        # gather around — so the wait_for can target the task directly.
        await asyncio.wait_for(asyncio.shield(task1), timeout=2.0)
        assert client1.is_orphaned
        # The first close event should carry the superseded reason.
        superseded_events = [c for c in closed if c["reason"] == "superseded"]
        assert len(superseded_events) >= 1
    finally:
        await cancel_and_drain(task2)
        if not task1.done():
            await cancel_and_drain(task1)


@pytest.mark.asyncio
async def test_peer_link_client_reconnects_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """A failed connect retries with backoff; succeed on second attempt.

    Drives one client against an initially-unreachable port,
    flips it to the live receiver after one failure, and asserts
    the client opens a session against the second target. Pins
    the auto-reconnect contract without waiting the full
    1-second backoff (monkey-patches the initial backoff to
    near-zero so the test finishes promptly).
    """
    monkeypatch.setattr(remote_build_peer_link_client, "_RECONNECT_INITIAL_BACKOFF_SECONDS", 0.01)

    server, receiver, _, receiver_pub = receiver_server
    initiator_priv = secrets.token_bytes(32)
    await _seed_approved_peer_for_initiator(
        receiver, dashboard_id="alpha", initiator_priv=initiator_priv
    )

    bus = EventBus()
    opened = asyncio.Event()
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)
    bus.add_listener(EventType.OFFLOADER_PEER_LINK_OPENED, lambda e: opened.set())

    # Start with a (mostly) unreachable port — connect to a
    # closed local port returns ECONNREFUSED quickly. Use the
    # client's own state to flip the target to the live port
    # after one failure lands.
    failed = asyncio.Event()

    class _RetargetingClient(PeerLinkClient):
        async def _run_one_session(self) -> str:  # type: ignore[override]
            reason = await super()._run_one_session()
            if reason == "transport_error" and not failed.is_set():
                self._port = server.port
                failed.set()
            return reason

    client = _RetargetingClient(
        receiver_hostname="127.0.0.1",
        receiver_port=1,  # privileged + closed; ECONNREFUSED
        identity_priv=initiator_priv,
        dashboard_id="alpha",
        pinned_static_x25519_pub=receiver_pub,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(opened.wait(), timeout=5.0)
        # First close was the transport error against port 1.
        assert any(c["reason"] == "transport_error" for c in closed)
    finally:
        await cancel_and_drain(task)


@pytest.mark.asyncio
async def test_run_session_loops_send_ping_routes_through_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``send_ping`` callback handed to the heartbeat sends a PING via the channel.

    Covers the body of the inner ``_send_ping`` closure inside
    :meth:`PeerLinkClient._run_session_loops`. The closure
    encrypts a ``{"type": "ping", "nonce": N}`` frame and writes
    it to the WS — it's small, but unstubbed-heartbeat tests
    don't otherwise reach it (the other heartbeat regressions
    here stub :func:`run_peer_link_heartbeat` to a no-op rather
    than driving the callback).
    """
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()
    ping_sent = asyncio.Event()

    sent_frames: list[bytes] = []

    class _RecordingWs(_ParkingWs):
        async def send_bytes(self, data: bytes) -> None:
            sent_frames.append(data)
            ping_sent.set()

    ws = _RecordingWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _ping_then_park(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await send_ping(42)
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _ping_then_park)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )
    drive_task = asyncio.create_task(client._run_session_loops(channel))
    await asyncio.wait_for(ping_sent.wait(), timeout=2.0)
    decoded = _json.loads(responder.decrypt(sent_frames[0]))
    assert decoded == {"type": "ping", "nonce": 42}

    closed_event.set()
    await drive_task


@pytest.mark.asyncio
async def test_run_session_loops_returns_heartbeat_timeout_when_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat ``on_dead`` callback writes ``heartbeat_timeout`` into the close reason.

    Regression: pre-fix the receive loop's ``async for`` exited
    after the heartbeat task closed the WS without overwriting
    ``close_reason``, so the bus event lied about the cause as
    ``peer_hung_up``. The fix uses :class:`_SessionLoopState`
    so heartbeat-driven closes write the real cause into a
    field both loops share.

    Stubs ``run_peer_link_heartbeat`` to invoke its provided
    ``on_dead`` immediately, then closes the parking WS — the
    receive loop exits and returns the populated close reason.
    No real timing involved.
    """
    initiator, _responder = _build_handshake_pair()
    closed_event = asyncio.Event()
    ws = _ParkingWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _fake_heartbeat(
        *,
        send_ping: Any,
        last_pong_at: Any,
        on_dead: Any,
    ) -> None:
        await on_dead()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _fake_heartbeat)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )

    close_reason = await client._run_session_loops(channel)

    assert close_reason == "heartbeat_timeout"


@pytest.mark.asyncio
async def test_peer_link_client_backoff_advances_when_session_never_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated never-opened sessions advance backoff exponentially; openings reset it.

    Regression: pre-fix the run loop checked
    ``_last_close_reason``, which only got set on the
    auth-rejected path; transport errors left it ``None`` and
    every iteration reset the backoff. The fix tracks
    ``_session_was_opened`` and only resets the backoff when
    the previous session reached ``intent_response: ok``.

    Stubs out ``_run_one_session`` so the test controls the
    sequence of "did this iteration open a session?" deterministically,
    and stubs ``asyncio.sleep`` to capture the requested
    backoff windows without actually sleeping.
    """
    initial = 1.0
    cap = 30.0
    monkeypatch.setattr(
        remote_build_peer_link_client, "_RECONNECT_INITIAL_BACKOFF_SECONDS", initial
    )
    monkeypatch.setattr(remote_build_peer_link_client, "_RECONNECT_MAX_BACKOFF_SECONDS", cap)

    backoffs_observed: list[float] = []
    real_sleep = asyncio.sleep

    async def _capturing_sleep(delay: float) -> None:
        if delay >= initial:
            backoffs_observed.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(remote_build_peer_link_client.asyncio, "sleep", _capturing_sleep)

    bus = EventBus()
    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )

    # Iteration plan: (was_opened, close_reason)
    #   1. transport_error, never opened → backoff doubles 1 → 2
    #   2. transport_error, never opened → backoff doubles 2 → 4
    #   3. peer_hung_up,    *opened*    → backoff resets to 1
    #   4. transport_error, never opened → backoff doubles 1 → 2
    plan = [
        (False, "transport_error"),
        (False, "transport_error"),
        (True, "peer_hung_up"),
        (False, "transport_error"),
    ]
    plan_iter = iter(plan)

    async def _fake_run_one_session() -> str:
        try:
            opened, reason = next(plan_iter)
        except StopIteration:
            # No more iterations — orphan the client so run()
            # exits cleanly.
            client._orphaned = True
            return "superseded"
        client._session_was_opened = opened
        return reason

    monkeypatch.setattr(client, "_run_one_session", _fake_run_one_session)

    await client.run()

    assert backoffs_observed == [2.0, 4.0, 1.0, 2.0]


def test_peer_link_client_exposes_receiver_coordinates() -> None:
    """``receiver_hostname`` / ``receiver_port`` properties echo the constructor args."""
    client = PeerLinkClient(
        receiver_hostname="10.0.0.5",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )
    assert client.receiver_hostname == "10.0.0.5"
    assert client.receiver_port == 6055


@pytest.mark.asyncio
async def test_peer_link_client_returns_transport_error_on_type_error(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``TypeError`` from the handshake (e.g. non-binary frame) maps to ``transport_error``.

    Covers the ``TypeError`` branch of the transport-exception
    catch in :meth:`PeerLinkClient._run_one_session`.
    aiohttp's ``ClientWebSocketResponse.receive_bytes()`` raises
    ``TypeError`` when the peer sends a TEXT frame or closes
    abruptly mid-handshake; without this branch the exception
    would bubble out and kill the long-lived task instead of
    triggering a reconnect.
    """
    server, _receiver, _, _ = receiver_server

    async def _typeerror_handshake(**kwargs: Any) -> bytes:
        raise TypeError("Received non-binary message")

    monkeypatch.setattr(
        remote_build_peer_link_client,
        "_drive_initiator_handshake_and_read_response",
        _typeerror_handshake,
    )

    bus = EventBus()
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(closed.received.wait(), timeout=2.0)
        assert closed[0]["reason"] == "transport_error"
    finally:
        await cancel_and_drain(task)


@pytest.mark.asyncio
async def test_peer_link_client_returns_transport_error_on_noise_failure(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Noise-side failure during the handshake maps to ``transport_error``.

    Covers the ``except NOISE_ERRORS`` catch in
    :meth:`PeerLinkClient._run_one_session`. Forces the failure
    by patching the shared handshake driver to raise a
    :class:`NoiseInvalidMessage` — any of the
    :data:`NOISE_ERRORS` tuple's types is sufficient.
    """
    server, _receiver, _, _ = receiver_server

    async def _bad_handshake(**kwargs: Any) -> bytes:
        raise NoiseInvalidMessage("forced for test")

    monkeypatch.setattr(
        remote_build_peer_link_client,
        "_drive_initiator_handshake_and_read_response",
        _bad_handshake,
    )

    bus = EventBus()
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(closed.received.wait(), timeout=2.0)
        assert closed[0]["reason"] == "transport_error"
    finally:
        await cancel_and_drain(task)


@pytest.mark.asyncio
async def test_peer_link_client_close_event_carries_error_detail_on_noise_failure(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport / Noise failure populates ``error_detail`` and ``last_connect_error``.

    Pins the operator-facing diagnostic shape: the
    category-level ``reason`` (``transport_error``) is
    accompanied by the specific exception text via
    ``error_detail``, and the same string is reachable
    sync-side via ``client.last_connect_error`` so a
    ``pairings_snapshot()`` read after the failure carries
    the message without the snapshot reader having to listen
    for the close event.
    """
    server, _receiver, _, _ = receiver_server

    async def _bad_handshake(**kwargs: Any) -> bytes:
        raise NoiseInvalidMessage("forced for test")

    monkeypatch.setattr(
        remote_build_peer_link_client,
        "_drive_initiator_handshake_and_read_response",
        _bad_handshake,
    )

    bus = EventBus()
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)
    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(closed.received.wait(), timeout=2.0)
        # Category code is unchanged; the new field carries the
        # specific exception text the operator wants to see.
        assert closed[0]["reason"] == "transport_error"
        assert closed[0]["error_detail"] == "NoiseInvalidMessage: forced for test"
        # Sync read on the client mirrors the same string so the
        # ``pairings_snapshot()`` projection in
        # ``RemoteBuildController`` doesn't need to listen for
        # close events to populate ``last_connect_error``.
        assert client.last_connect_error == "NoiseInvalidMessage: forced for test"
        # The reconnect loop is still alive (this isn't an
        # orphan path); ``is_connecting`` reports the live state
        # between attempts.
        assert client.is_connecting is True
        assert client.is_orphaned is False
    finally:
        await cancel_and_drain(task)


@pytest.mark.asyncio
async def test_peer_link_client_auth_rejected_when_dashboard_id_unknown(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """An unapproved dashboard_id fires CLOSED with auth_rejected.

    Drives a real handshake against the receiver but skips the
    ``_seed_approved_peer_for_initiator`` step. The receiver
    responds ``intent_response: not_paired``; the client surfaces
    that as the offloader-side ``auth_rejected`` reason on
    ``OFFLOADER_PEER_LINK_CLOSED``.
    """
    server, _receiver, _, receiver_pub = receiver_server
    bus = EventBus()
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="never-paired",
        pinned_static_x25519_pub=receiver_pub,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(closed.received.wait(), timeout=2.0)
        assert closed[0]["reason"] == "auth_rejected"
    finally:
        await cancel_and_drain(task)


@pytest.mark.asyncio
async def test_peer_link_client_pin_mismatch_aborts_and_orphans(
    receiver_server: tuple[TestServer, RemoteBuildController, str, bytes],
) -> None:
    """A pinned pubkey that doesn't match the receiver's actual key aborts the connect.

    Pin-check (4a-o part 5) — the offloader's outbound
    ``peer_link`` handshake must compare ``session.remote_static_pub``
    against the value OOB-confirmed during preview. Drives a
    real handshake against the receiver but passes the WRONG
    ``pinned_static_x25519_pub``: the server's static key is
    legitimate, but the client thinks it's expecting a different
    one (simulating an mDNS spoof or an attacker on the wire).

    The client must:

    1. Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` with the diagnostic
       payload (expected vs. observed pin).
    2. Fire ``OFFLOADER_PEER_LINK_CLOSED`` with ``reason="pin_mismatch"``.
    3. Orphan itself so the reconnect loop doesn't keep
       hammering whatever's at the wrong endpoint.
    4. NOT fire ``OFFLOADER_PEER_LINK_OPENED`` — application
       frames must not flow against the wrong identity.
    """
    server, receiver, _, receiver_pub = receiver_server
    initiator_priv = secrets.token_bytes(32)
    await _seed_approved_peer_for_initiator(
        receiver, dashboard_id="alpha", initiator_priv=initiator_priv
    )

    bus = EventBus()
    opened = capture_events(bus, EventType.OFFLOADER_PEER_LINK_OPENED)
    closed = capture_events(bus, EventType.OFFLOADER_PEER_LINK_CLOSED)
    pin_mismatch = capture_events(bus, EventType.OFFLOADER_PAIR_PIN_MISMATCH)

    # Wrong pinned pubkey: flip one bit of the receiver's actual
    # pubkey so the result is guaranteed-different. Reversing the
    # bytes would have a vanishingly small but non-zero chance of
    # producing the same value on a self-palindromic key; the
    # XOR-with-0x01 form has none.
    wrong_pub = bytes([receiver_pub[0] ^ 0x01]) + receiver_pub[1:]
    assert wrong_pub != receiver_pub

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        identity_priv=initiator_priv,
        dashboard_id="alpha",
        pinned_static_x25519_pub=wrong_pub,
        pin_sha256="a" * 64,
        receiver_label="my-laptop",
        bus=bus,
    )
    task = asyncio.create_task(client.run())
    try:
        # Pin-mismatch fires before close; close fires before
        # orphaning. Wait on close (the terminal signal).
        await asyncio.wait_for(closed.received.wait(), timeout=2.0)
        # Yield once so any pending ``run`` post-close work
        # (orphan flag set, return) finishes before we assert.
        for _ in range(10):
            if task.done():
                break
            await asyncio.sleep(0)

        assert closed[0]["reason"] == "pin_mismatch"
        assert len(pin_mismatch) == 1
        assert pin_mismatch[0]["receiver_hostname"] == "127.0.0.1"
        assert pin_mismatch[0]["receiver_label"] == "my-laptop"
        assert pin_mismatch[0]["expected_pin"] != pin_mismatch[0]["observed_pin"]
        # Application channel never opened — bundles can't flow
        # against the wrong identity.
        assert len(opened) == 0
        # Client orphaned: reconnect loop exits without further
        # backoff. ``run`` has returned, so the task is done.
        assert task.done()
        assert client.is_orphaned is True
    finally:
        await cancel_and_drain(task)


@pytest.mark.asyncio
async def test_run_session_loops_responds_to_peer_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PING from the receiver is answered with a PONG and the loop keeps running."""
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()
    pong_sent = asyncio.Event()

    sent_frames: list[bytes] = []

    class _PingingWs(_ParkingWs):
        def __init__(self, evt: asyncio.Event) -> None:
            super().__init__(evt)
            self._delivered = False

        async def send_bytes(self, data: bytes) -> None:
            sent_frames.append(data)
            pong_sent.set()

        async def __anext__(self) -> Any:
            if not self._delivered:
                self._delivered = True
                ping = responder.encrypt(_json.dumps({"type": "ping", "nonce": 7}))
                return WSMessage(type=WSMsgType.BINARY, data=ping, extra="")
            await self._closed_event.wait()
            raise StopAsyncIteration

    ws = _PingingWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _idle_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _idle_heartbeat)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )

    async def _drive() -> str:
        return await client._run_session_loops(channel)

    drive_task = asyncio.create_task(_drive())
    await asyncio.wait_for(pong_sent.wait(), timeout=2.0)
    decoded = _json.loads(responder.decrypt(sent_frames[0]))
    assert decoded == {"type": "pong", "nonce": 7}

    closed_event.set()
    reason = await drive_task
    assert reason == "peer_hung_up"


@pytest.mark.asyncio
async def test_run_session_loops_bumps_last_pong_on_pong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PONG from the receiver bumps ``last_pong_at`` so the next heartbeat sees fresh activity."""
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()

    class _PongingWs(_ParkingWs):
        def __init__(self, evt: asyncio.Event) -> None:
            super().__init__(evt)
            self._delivered = False

        async def __anext__(self) -> Any:
            if not self._delivered:
                self._delivered = True
                pong = responder.encrypt(_json.dumps({"type": "pong", "nonce": 1}))
                return WSMessage(type=WSMsgType.BINARY, data=pong, extra="")
            await self._closed_event.wait()
            raise StopAsyncIteration

    ws = _PongingWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    captured_last_pong: list[float] = []
    sample_taken = asyncio.Event()

    async def _capturing_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        # Sleep one tick so the receive loop processes the PONG first.
        await asyncio.sleep(0)
        captured_last_pong.append(last_pong_at())
        sample_taken.set()
        await closed_event.wait()

    monkeypatch.setattr(
        remote_build_peer_link_client, "run_peer_link_heartbeat", _capturing_heartbeat
    )

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )
    drive_task = asyncio.create_task(client._run_session_loops(channel))

    await asyncio.wait_for(sample_taken.wait(), timeout=2.0)

    closed_event.set()
    reason = await drive_task

    assert reason == "peer_hung_up"
    assert captured_last_pong, "heartbeat callback never sampled last_pong_at"
    # The captured value should be a real loop timestamp; we
    # mostly care that the PONG branch ran without falling
    # through to the unknown-msg-type branch (which would have
    # left ``last_pong_at`` at the initial value).


@pytest.mark.asyncio
async def test_run_session_loops_returns_transport_error_on_malformed_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame that ``parse_frame`` rejects ends the loop with ``transport_error``."""
    initiator, _responder = _build_handshake_pair()
    closed_event = asyncio.Event()

    class _GarbageWs(_ParkingWs):
        def __init__(self, evt: asyncio.Event) -> None:
            super().__init__(evt)
            self._delivered = False

        async def __anext__(self) -> Any:
            if not self._delivered:
                self._delivered = True
                # Bytes that won't decrypt — parse_frame returns None.
                return WSMessage(type=WSMsgType.BINARY, data=b"\x00" * 32, extra="")
            await self._closed_event.wait()
            raise StopAsyncIteration

    ws = _GarbageWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _idle_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _idle_heartbeat)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )
    reason = await client._run_session_loops(channel)
    assert reason == "transport_error"


@pytest.mark.asyncio
async def test_run_session_loops_ignores_unknown_msg_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown ``type`` in a valid frame is logged and ignored; the loop keeps running."""
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()

    class _UnknownTypeWs(_ParkingWs):
        def __init__(self, evt: asyncio.Event) -> None:
            super().__init__(evt)
            self._delivered = False

        async def __anext__(self) -> Any:
            if not self._delivered:
                self._delivered = True
                frame = responder.encrypt(_json.dumps({"type": "wat", "payload": 1}))
                return WSMessage(type=WSMsgType.BINARY, data=frame, extra="")
            await self._closed_event.wait()
            raise StopAsyncIteration

    ws = _UnknownTypeWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _idle_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _idle_heartbeat)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )
    drive_task = asyncio.create_task(client._run_session_loops(channel))
    # The fake WS delivers the unknown frame on the first
    # ``__anext__`` and then awaits ``closed_event`` on the next
    # call. Setting it now lets the receive loop process the
    # unknown frame (logged + ignored) and exit cleanly.
    closed_event.set()
    reason = await drive_task
    # Default close reason — neither malformed nor terminate; the
    # unknown frame was logged and dropped, the loop fell through
    # to the WS close.
    assert reason == "peer_hung_up"


@pytest.mark.asyncio
async def test_run_session_loops_on_dead_swallows_aiohttp_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat-driven close that raises ``aiohttp.ClientError`` doesn't crash the task.

    Regression: the offloader-side ``_on_dead`` callback in
    :meth:`PeerLinkClient._run_session_loops` originally
    suppressed only ``OSError`` / ``RuntimeError`` around
    ``ws.close()``. ``aiohttp.ClientWebSocketResponse.close()``
    can raise ``ClientError`` when the peer has already
    disappeared; that exception would crash the heartbeat task
    and let the receive loop fall through to its
    ``peer_hung_up`` default, masking the real
    ``heartbeat_timeout`` cause.

    Forces the failure by replacing the parking WS's ``close``
    with one that raises ``aiohttp.ClientConnectionError``;
    drives the receive loop via a stub heartbeat that fires
    ``on_dead`` immediately; asserts the close reason is still
    ``heartbeat_timeout``.
    """
    initiator, _responder = _build_handshake_pair()
    closed_event = asyncio.Event()

    class _RaisingCloseWs(_ParkingWs):
        async def close(self) -> None:
            self.closed = True
            self._closed_event.set()
            raise aiohttp.ClientConnectionError("forced for test")

    ws = _RaisingCloseWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _fire_on_dead(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await on_dead()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _fire_on_dead)

    client = PeerLinkClient(
        receiver_hostname="127.0.0.1",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=EventBus(),
    )

    close_reason = await client._run_session_loops(channel)
    assert close_reason == "heartbeat_timeout"


# ---------------------------------------------------------------------------
# Phase 5a-2 — RemoteBuildController peer-link client task lifecycle.
# ---------------------------------------------------------------------------


def _prime_offloader_identity_for_spawn(controller: RemoteBuildController) -> None:
    """Set the identity prerequisites :meth:`_spawn_peer_link_client` checks before spawning.

    Production wires these in :meth:`start`; tests that bypass
    ``start`` can call this helper to flip the same flags
    directly. ``_db.bus`` is a separate prerequisite — set it
    explicitly on the test (typically to a ``MagicMock`` or
    ``EventBus`` depending on what's being asserted).
    """
    controller._offloader_dashboard_id = "test-dashboard"
    controller._offloader_peer_link_priv = secrets.token_bytes(32)


@pytest.mark.asyncio
async def test_await_pair_status_flip_self_removes_listener_on_terminal_exit(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listener that exits via terminal result deletes its own slot in ``_pair_status_listeners``.

    Covers the ``self._pair_status_listeners.get(key) is asyncio.current_task()``
    branch's ``del`` line in :meth:`_await_pair_status_flip`'s
    ``finally``. The slot is only removed when nobody else has
    replaced the task in the meantime — re-pair flows pop the
    old task from the slot before installing the new one, so
    the old task's finally must NOT pop the replacement (the
    rationale documented inline at the production site).
    """

    async def _approved_round_trip(**kwargs: Any) -> PairStatusResult:
        return PairStatusResult(status=IntentResponse.APPROVED, pin_sha256="a" * 64)

    monkeypatch.setattr(rb, "peer_link_await_pair_status", _approved_round_trip)

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pubkey = b"\xab" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        status=PeerStatus.PENDING,
    )
    offloader._pairings[pin] = pairing
    offloader._spawn_pair_status_listener(pairing)
    listener = offloader._pair_status_listeners[pin]

    await listener

    assert pin not in offloader._pair_status_listeners


@pytest.mark.asyncio
async def test_spawn_peer_link_client_idempotent_when_task_running(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second :meth:`_spawn_peer_link_client` for the same key is a no-op.

    Covers the ``existing is not None and not existing.done()``
    early-return branch in :meth:`_spawn_peer_link_client`.
    Without it, an apply-pair-status-result that lands twice (e.g.
    a re-pair race against an already-running client) would
    replace the live task and leak the original.
    """
    park = asyncio.Event()

    async def _parked_run(self: PeerLinkClient) -> None:
        await park.wait()

    monkeypatch.setattr(PeerLinkClient, "run", _parked_run)

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    _prime_offloader_identity_for_spawn(offloader)
    pairing = _stub_pairing(
        receiver_hostname="rcv.local", receiver_port=6055, status=PeerStatus.APPROVED
    )

    offloader._spawn_peer_link_client(pairing)
    await asyncio.sleep(0)
    first_handle = offloader._peer_link_clients["a" * 64]

    offloader._spawn_peer_link_client(pairing)
    await asyncio.sleep(0)

    assert offloader._peer_link_clients["a" * 64] is first_handle
    park.set()
    await cancel_and_drain(first_handle.task)


@pytest.mark.asyncio
async def test_cancel_peer_link_client_cancels_running_task(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """:meth:`_cancel_peer_link_client` pops + cancels a running task.

    Covers the ``task is not None and not task.done()`` branch
    (including the ``task.cancel()`` line) in
    :meth:`_cancel_peer_link_client`. The unpair path relies on
    this to close the long-lived Noise WS promptly when the user
    drops a pairing.
    """
    park = asyncio.Event()

    async def _parked_run(self: PeerLinkClient) -> None:
        await park.wait()

    monkeypatch.setattr(PeerLinkClient, "run", _parked_run)

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    _prime_offloader_identity_for_spawn(offloader)
    pairing = _stub_pairing(
        receiver_hostname="rcv.local", receiver_port=6055, status=PeerStatus.APPROVED
    )
    offloader._spawn_peer_link_client(pairing)
    await asyncio.sleep(0)
    handle = offloader._peer_link_clients["a" * 64]

    offloader._cancel_peer_link_client("a" * 64)

    assert "a" * 64 not in offloader._peer_link_clients
    with pytest.raises(asyncio.CancelledError):
        await handle.task


@pytest.mark.asyncio
async def test_stop_drains_peer_link_clients(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """:meth:`stop` cancels every peer-link client task and clears the registry.

    Covers the drain loop at the top of :meth:`stop` —
    ``for task in self._peer_link_clients.values(): task.cancel()``
    plus the ``await asyncio.gather(...)`` and the subsequent
    ``self._peer_link_clients.clear()``.
    """
    park = asyncio.Event()

    async def _parked_run(self: PeerLinkClient) -> None:
        await park.wait()

    monkeypatch.setattr(PeerLinkClient, "run", _parked_run)

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    _prime_offloader_identity_for_spawn(offloader)
    for host, port, pin in (
        ("a.local", 6055, "a" * 64),
        ("b.local", 6055, "b" * 64),
    ):
        offloader._spawn_peer_link_client(
            _stub_pairing(
                receiver_hostname=host,
                receiver_port=port,
                pin_sha256=pin,
                status=PeerStatus.APPROVED,
            )
        )
    await asyncio.sleep(0)
    tasks = [h.task for h in offloader._peer_link_clients.values()]
    assert len(tasks) == 2

    await offloader.stop()

    assert offloader._peer_link_clients == {}
    for task in tasks:
        assert task.done()
    # ``stop()`` already drained these tasks; gather one more time
    # with ``return_exceptions=True`` so the test absorbs each
    # task's captured ``CancelledError`` without it surfacing.
    await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Phase 5b: queue_status receive handling on the offloader-side client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_session_loops_fires_queue_status_event_on_inbound_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``queue_status`` frame from the receiver fires ``OFFLOADER_QUEUE_STATUS_CHANGED``."""
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()

    class _QueueStatusWs(_ParkingWs):
        def __init__(self, evt: asyncio.Event) -> None:
            super().__init__(evt)
            self._delivered = False

        async def __anext__(self) -> Any:
            if not self._delivered:
                self._delivered = True
                frame = responder.encrypt(
                    _json.dumps(
                        {
                            "type": "queue_status",
                            "idle": False,
                            "running": True,
                            "queue_depth": 3,
                        }
                    )
                )
                return WSMessage(type=WSMsgType.BINARY, data=frame, extra="")
            await self._closed_event.wait()
            raise StopAsyncIteration

    ws = _QueueStatusWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _idle_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _idle_heartbeat)

    bus = EventBus()
    captured = capture_events(bus, EventType.OFFLOADER_QUEUE_STATUS_CHANGED)

    client = PeerLinkClient(
        receiver_hostname="receiver.local",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    drive_task = asyncio.create_task(client._run_session_loops(channel))

    await asyncio.wait_for(captured.received.wait(), timeout=2.0)
    closed_event.set()
    reason = await drive_task
    assert reason == "peer_hung_up"

    assert len(captured) == 1
    payload = captured[0]
    assert payload == {
        "receiver_hostname": "receiver.local",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "idle": False,
        "running": True,
        "queue_depth": 3,
    }


@pytest.mark.parametrize(
    "frame_body",
    [
        # idle is not bool
        {"type": "queue_status", "idle": "no", "running": True, "queue_depth": 1},
        # running is missing
        {"type": "queue_status", "idle": False, "queue_depth": 1},
        # queue_depth is a string
        {"type": "queue_status", "idle": False, "running": True, "queue_depth": "two"},
        # queue_depth is bool (which would otherwise pass the int check
        # because bool is a subclass of int)
        {"type": "queue_status", "idle": False, "running": True, "queue_depth": True},
    ],
    ids=[
        "non-bool-idle",
        "missing-running",
        "non-int-queue-depth",
        "bool-queue-depth",
    ],
)
@pytest.mark.asyncio
async def test_run_session_loops_drops_malformed_queue_status(
    monkeypatch: pytest.MonkeyPatch,
    frame_body: dict[str, Any],
) -> None:
    """A malformed ``queue_status`` frame is dropped without firing the event."""
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()

    class _BadQueueStatusWs(_ParkingWs):
        def __init__(self, evt: asyncio.Event) -> None:
            super().__init__(evt)
            self._delivered = False

        async def __anext__(self) -> Any:
            if not self._delivered:
                self._delivered = True
                frame = responder.encrypt(_json.dumps(frame_body))
                return WSMessage(type=WSMsgType.BINARY, data=frame, extra="")
            await self._closed_event.wait()
            raise StopAsyncIteration

    ws = _BadQueueStatusWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _idle_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _idle_heartbeat)

    bus = EventBus()
    captured = capture_events(bus, EventType.OFFLOADER_QUEUE_STATUS_CHANGED)

    client = PeerLinkClient(
        receiver_hostname="receiver.local",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    drive_task = asyncio.create_task(client._run_session_loops(channel))
    # Yield long enough for the malformed-frame branch to run
    # and drop the frame; then close out cleanly.
    for _ in range(10):
        await asyncio.sleep(0)
    closed_event.set()
    reason = await drive_task

    assert reason == "peer_hung_up"
    assert len(captured) == 0


# ---------------------------------------------------------------------------
# Phase 5c-3: offloader-side submit_job + ack/state/output dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_session_loops_fires_offloader_job_state_changed_on_inbound_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``job_state_changed`` frame fires ``OFFLOADER_JOB_STATE_CHANGED`` with peer coords."""
    bus = EventBus()
    captured = capture_events(bus, EventType.OFFLOADER_JOB_STATE_CHANGED)
    client = _make_offloader_client(bus)
    frame = {
        "type": "job_state_changed",
        "job_id": "j-001",
        "status": "running",
        "error_message": "",
    }
    async with _drive_session_with_frames(client, monkeypatch, [frame]):
        await asyncio.wait_for(captured.received.wait(), timeout=2.0)

    assert len(captured) == 1
    assert captured[0] == {
        "receiver_hostname": "receiver.local",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "job_id": "j-001",
        "status": "running",
        "error_message": "",
    }


@pytest.mark.parametrize(
    "frame_body",
    [
        # invalid status literal
        {
            "type": "job_state_changed",
            "job_id": "j-001",
            "status": "garbage",
            "error_message": "",
        },
        # missing error_message
        {"type": "job_state_changed", "job_id": "j-001", "status": "running"},
        # job_id wrong type
        {
            "type": "job_state_changed",
            "job_id": 42,
            "status": "running",
            "error_message": "",
        },
    ],
    ids=["invalid-status", "missing-error_message", "non-string-job_id"],
)
@pytest.mark.asyncio
async def test_run_session_loops_drops_malformed_job_state_changed(
    monkeypatch: pytest.MonkeyPatch,
    frame_body: dict[str, Any],
) -> None:
    """Malformed ``job_state_changed`` frames are dropped without firing the event."""
    bus = EventBus()
    captured = capture_events(bus, EventType.OFFLOADER_JOB_STATE_CHANGED)
    client = _make_offloader_client(bus)
    async with _drive_session_with_frames(client, monkeypatch, [frame_body]):
        # Yield long enough for the malformed-frame branch to run
        # and drop the frame; the context manager handles close.
        for _ in range(10):
            await asyncio.sleep(0)
    assert len(captured) == 0


@pytest.mark.asyncio
async def test_run_session_loops_fires_offloader_job_output_on_inbound_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``job_output`` frame fires ``OFFLOADER_JOB_OUTPUT`` and preserves the terminator."""
    bus = EventBus()
    captured = capture_events(bus, EventType.OFFLOADER_JOB_OUTPUT)
    client = _make_offloader_client(bus)
    frame = {
        "type": "job_output",
        "job_id": "j-002",
        "stream": "stdout",
        "line": "Compiling kitchen.cpp\n",
    }
    async with _drive_session_with_frames(client, monkeypatch, [frame]):
        await asyncio.wait_for(captured.received.wait(), timeout=2.0)

    assert len(captured) == 1
    assert captured[0] == {
        "receiver_hostname": "receiver.local",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "job_id": "j-002",
        "stream": "stdout",
        "line": "Compiling kitchen.cpp\n",
    }


@pytest.mark.asyncio
async def test_run_session_loops_resolves_submit_job_ack_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``submit_job_ack`` frame fires the matching ack future with the parsed payload."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    # Pre-register the ack future under the same job_id the
    # synthetic frame carries — mirrors what
    # :meth:`PeerLinkClient.submit_job` does just before sending.
    ack_fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._submit_job_acks["j-acked"] = ack_fut

    frame = {
        "type": "submit_job_ack",
        "job_id": "j-acked",
        "accepted": False,
        "reason": "queue_rejected",
    }
    async with _drive_session_with_frames(client, monkeypatch, [frame]):
        ack = await asyncio.wait_for(ack_fut, timeout=2.0)

    assert ack == frame


@pytest.mark.asyncio
async def test_run_session_loops_finally_drains_pending_submit_acks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending ack futures are completed with :class:`SubmitJobSessionLostError` on session end."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    pending: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._submit_job_acks["abandoned"] = pending

    async with _drive_session_with_frames(client, monkeypatch, []):
        # Let the receive loop park and set ``_active_channel``;
        # context exit closes the iterator so the ``finally``
        # drains the pending future.
        await asyncio.sleep(0)

    with pytest.raises(SubmitJobSessionLostError):
        await pending


@pytest.mark.asyncio
async def test_submit_job_raises_no_session_error_when_session_closed() -> None:
    """:meth:`submit_job` without a live session raises :class:`PeerLinkNoSessionError`."""
    client = _make_offloader_client(EventBus())
    assert not client.is_session_open
    with pytest.raises(PeerLinkNoSessionError):
        await client.submit_job(
            job_id="j-1",
            configuration_filename="kitchen.yaml",
            target="compile",
            bundle_bytes=b"some-bundle-bytes",
        )


@pytest.mark.asyncio
async def test_submit_job_sends_header_chunks_and_returns_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: :meth:`submit_job` sends header + chunks and resolves on ack."""
    initiator, responder = _build_handshake_pair()
    closed_event = asyncio.Event()
    sent_frames: list[dict[str, Any]] = []
    ack_event = asyncio.Event()

    class _AckOnLastChunkWs(_ParkingWs):
        async def send_bytes(self, data: bytes) -> None:
            # Decrypt sent frames so the test asserts on
            # plaintext payloads (not Noise ciphertexts).
            plaintext = responder.decrypt(data)
            payload = _json.loads(plaintext)
            sent_frames.append(payload)
            if payload.get("type") == "submit_job_chunk" and payload.get("is_last") is True:
                ack_event.set()

        async def __anext__(self) -> Any:
            await ack_event.wait()
            # After all chunks are received, deliver an ack.
            if not getattr(self, "_acked", False):
                self._acked = True
                ack_frame = responder.encrypt(
                    _json.dumps(
                        {
                            "type": "submit_job_ack",
                            "job_id": "j-success",
                            "accepted": True,
                        }
                    )
                )
                return WSMessage(type=WSMsgType.BINARY, data=ack_frame, extra="")
            await self._closed_event.wait()
            raise StopAsyncIteration

    ws = _AckOnLastChunkWs(closed_event)
    channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    async def _idle_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        await closed_event.wait()

    monkeypatch.setattr(remote_build_peer_link_client, "run_peer_link_heartbeat", _idle_heartbeat)

    bus = EventBus()
    client = _make_offloader_client(bus)
    drive_task = asyncio.create_task(client._run_session_loops(channel))
    # Wait for the receive loop to park (sets ``_active_channel``).
    while not client.is_session_open:
        await asyncio.sleep(0)
    bundle = b"x" * (32 * 1024 * 2 + 17)  # 2 full chunks + a tail
    ack = await client.submit_job(
        job_id="j-success",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=bundle,
    )
    closed_event.set()
    await drive_task

    assert ack == {
        "type": "submit_job_ack",
        "job_id": "j-success",
        "accepted": True,
    }
    # First frame: the header.
    assert sent_frames[0]["type"] == "submit_job"
    assert sent_frames[0]["job_id"] == "j-success"
    assert sent_frames[0]["configuration_filename"] == "kitchen.yaml"
    assert sent_frames[0]["target"] == "compile"
    assert sent_frames[0]["total_bundle_bytes"] == len(bundle)
    assert sent_frames[0]["num_chunks"] == 3
    assert sent_frames[0]["bundle_sha256"] == hashlib.sha256(bundle).hexdigest()
    # Remaining frames: 3 chunks, with monotonic indices and is_last on the tail.
    chunks = [f for f in sent_frames if f.get("type") == "submit_job_chunk"]
    assert [c["chunk_index"] for c in chunks] == [0, 1, 2]
    assert [c["is_last"] for c in chunks] == [False, False, True]


@pytest.mark.asyncio
async def test_submit_job_times_out_when_no_ack_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an ack, :meth:`submit_job` raises :class:`SubmitJobTimeoutError`.

    The timeout constant is monkeypatched down so the test
    finishes quickly; production keeps the 60s wall.
    """
    monkeypatch.setattr(remote_build_peer_link_client, "_SUBMIT_JOB_ACK_TIMEOUT_SECONDS", 0.05)
    bus = EventBus()
    client = _make_offloader_client(bus)
    # No frames delivered — the receive loop parks immediately,
    # the chunk sends land on the no-op send_bytes, and
    # ``submit_job`` waits for an ack that never arrives.
    async with _drive_session_with_frames(client, monkeypatch, []):
        while not client.is_session_open:
            await asyncio.sleep(0)
        with pytest.raises(SubmitJobTimeoutError):
            await client.submit_job(
                job_id="j-timeout",
                configuration_filename="kitchen.yaml",
                target="compile",
                bundle_bytes=b"data",
            )


@pytest.mark.asyncio
async def test_submit_job_rejects_duplicate_job_id() -> None:
    """A second :meth:`submit_job` with an in-flight ``job_id`` raises immediately."""
    client = _make_offloader_client(EventBus())
    # Spoof an open session so the no-session branch is skipped.
    initiator, _responder = _build_handshake_pair()
    closed_event = asyncio.Event()
    client._active_channel = PeerLinkChannel(
        noise=initiator, ws=_ParkingWs(closed_event), log_label="127.0.0.1:6055"
    )
    # Pre-register a future under the id we'll re-submit against.
    client._submit_job_acks["j-dup"] = asyncio.get_running_loop().create_future()
    with pytest.raises(PeerLinkNoSessionError):
        await client.submit_job(
            job_id="j-dup",
            configuration_filename="kitchen.yaml",
            target="compile",
            bundle_bytes=b"data",
        )


# ---------------------------------------------------------------------------
# Phase 5c-3: controller WS command (remote_build/submit_job)
# ---------------------------------------------------------------------------


def _seed_open_peer_link_client(
    offloader: RemoteBuildController, pairing: StoredPairing
) -> PeerLinkClient:
    """Seed *offloader* with a fake open peer-link client for *pairing*.

    Skips the actual ``run`` task — installs a stub
    :class:`PeerLinkClient` with ``is_session_open=True`` and
    parks a ``done`` task on the handle so
    ``_lookup_open_peer_link_client`` finds it. Returns the
    client object so the caller can monkeypatch ``submit_job``.
    """
    client = _make_offloader_client(
        MagicMock(),
        receiver_hostname=pairing.receiver_hostname,
        receiver_port=pairing.receiver_port,
        pinned_static_x25519_pub=pairing.static_x25519_pub,
        pin_sha256=pairing.pin_sha256,
        receiver_label=pairing.label,
    )
    # Spoof a live session — passes ``is_session_open``.
    initiator, _responder = _build_handshake_pair()
    closed = asyncio.Event()
    client._active_channel = PeerLinkChannel(
        noise=initiator,
        ws=_ParkingWs(closed),
        log_label=f"{pairing.receiver_hostname}:{pairing.receiver_port}",
    )
    # Use a no-op task so handle.task.done() is False (we want
    # the lookup to consider the client live).
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    task: asyncio.Task[None] = asyncio.create_task(_park())
    offloader._peer_link_clients[pairing.pin_sha256] = rb._PeerLinkClientHandle(
        client=client, task=task
    )
    # Caller is responsible for cancelling ``task`` at end-of-test.
    return client


@pytest.mark.asyncio
async def test_controller_submit_job_returns_ack_on_accept(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: validates input, builds bundle, sends, and returns the ack shape."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[pairing.pin_sha256] = pairing
    # Drop a stub YAML at the expected path so ``rel_path`` resolves.
    yaml_path = Path(offloader._db.settings.config_dir) / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_build_bundle(_path: Path) -> bytes:
        return b"bundle-bytes"

    captured_args: dict[str, Any] = {}

    async def _stub_submit_job(
        *,
        job_id: str,
        configuration_filename: str,
        target: Any,
        bundle_bytes: bytes,
    ) -> dict[str, Any]:
        captured_args["job_id"] = job_id
        captured_args["configuration_filename"] = configuration_filename
        captured_args["target"] = target
        captured_args["bundle_bytes"] = bundle_bytes
        return {"type": "submit_job_ack", "job_id": job_id, "accepted": True}

    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_bundle.build_yaml_bundle",
        _stub_build_bundle,
    )
    monkeypatch.setattr(client, "submit_job", _stub_submit_job)

    try:
        result = await offloader.submit_job(
            pin_sha256=pairing.pin_sha256,
            configuration="kitchen.yaml",
            target="compile",
        )
    finally:
        # Drain the parked task spun up by ``_seed_open_peer_link_client``.
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )

    assert result == {"job_id": captured_args["job_id"], "accepted": True}
    assert captured_args["configuration_filename"] == "kitchen.yaml"
    assert captured_args["target"] == "compile"
    assert captured_args["bundle_bytes"] == b"bundle-bytes"


@pytest.mark.asyncio
async def test_controller_submit_job_passes_through_reject_reason(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-accepted ack lands as ``{accepted: False, reason: ...}`` to the WS caller."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[pairing.pin_sha256] = pairing
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_build_bundle(_path: Path) -> bytes:
        return b"bundle-bytes"

    async def _stub_submit_job(**kwargs: Any) -> dict[str, Any]:
        return {
            "type": "submit_job_ack",
            "job_id": kwargs["job_id"],
            "accepted": False,
            "reason": "queue_rejected",
        }

    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_bundle.build_yaml_bundle",
        _stub_build_bundle,
    )
    monkeypatch.setattr(client, "submit_job", _stub_submit_job)

    try:
        result = await offloader.submit_job(
            pin_sha256=pairing.pin_sha256,
            configuration="kitchen.yaml",
            target="upload",
        )
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )

    assert result["accepted"] is False
    assert result["reason"] == "queue_rejected"


@pytest.mark.asyncio
async def test_controller_submit_job_invalid_target_raises_invalid_args(
    offloader_controller_dir: Path,
) -> None:
    """A bad ``target`` value short-circuits with INVALID_ARGS before any wire activity."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    with pytest.raises(CommandError) as exc_info:
        await offloader.submit_job(
            pin_sha256="a" * 64,
            configuration="kitchen.yaml",
            target="install",  # not in {compile, upload}
        )
    assert exc_info.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_controller_submit_job_unknown_pairing_raises_not_found(
    offloader_controller_dir: Path,
) -> None:
    """No pairing under the given pin → NOT_FOUND."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    with pytest.raises(CommandError) as exc_info:
        await offloader.submit_job(
            pin_sha256="b" * 64,
            configuration="kitchen.yaml",
            target="compile",
        )
    assert exc_info.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_controller_submit_job_pending_pairing_raises_precondition_failed(
    offloader_controller_dir: Path,
) -> None:
    """A PENDING pairing rejects with PRECONDITION_FAILED."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.PENDING)
    offloader._pairings[pairing.pin_sha256] = pairing
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    with pytest.raises(CommandError) as exc_info:
        await offloader.submit_job(
            pin_sha256=pairing.pin_sha256,
            configuration="kitchen.yaml",
            target="compile",
        )
    assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED


@pytest.mark.asyncio
async def test_controller_submit_job_no_session_raises_precondition_failed(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approved pairing but no live session → PRECONDITION_FAILED."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[pairing.pin_sha256] = pairing
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    # No client spawned at all → PRECONDITION_FAILED before bundling.
    with pytest.raises(CommandError) as exc_info:
        await offloader.submit_job(
            pin_sha256=pairing.pin_sha256,
            configuration="kitchen.yaml",
            target="compile",
        )
    assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED


@pytest.mark.asyncio
async def test_controller_submit_job_timeout_maps_to_unavailable(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A :class:`SubmitJobTimeoutError` raised by the client → CommandError(UNAVAILABLE)."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[pairing.pin_sha256] = pairing
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_build_bundle(_path: Path) -> bytes:
        return b"bundle-bytes"

    async def _stub_submit_job(**kwargs: Any) -> dict[str, Any]:
        raise SubmitJobTimeoutError("ack timed out")

    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_bundle.build_yaml_bundle",
        _stub_build_bundle,
    )
    monkeypatch.setattr(client, "submit_job", _stub_submit_job)

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.submit_job(
                pin_sha256=pairing.pin_sha256,
                configuration="kitchen.yaml",
                target="compile",
            )
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_controller_submit_job_empty_configuration_raises_invalid_args(
    offloader_controller_dir: Path,
) -> None:
    """An empty ``configuration`` arg gets rejected upfront."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    with pytest.raises(CommandError) as exc_info:
        await offloader.submit_job(
            pin_sha256="a" * 64,
            configuration="",
            target="compile",
        )
    assert exc_info.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_controller_submit_job_rejects_path_traversal(
    make_settings: MakeSettingsFactory,
) -> None:
    """Path-traversal ``configuration`` arg rejected by ``rel_path`` as INVALID_ARGS.

    Uses the real :class:`DashboardSettings` (not the
    :class:`MagicMock` stub from ``_make_offloader_controller``)
    so :meth:`DashboardSettings.rel_path` actually performs its
    ``relative_to(absolute_config_dir)`` check. The MagicMock-
    stubbed Settings lets ``rel_path`` return a ``MagicMock``
    silently — fine for tests that don't care about the
    boundary, dangerous if those were the only tests covering
    the boundary.
    """
    settings = make_settings()
    db = MagicMock()
    db.devices = MagicMock()
    db.devices.zeroconf = None
    db._dashboard_advertiser = None
    db.settings = settings
    offloader = RemoteBuildController(db)
    offloader._db.bus = MagicMock()

    with pytest.raises(CommandError) as exc_info:
        await offloader.submit_job(
            pin_sha256="a" * 64,
            configuration="../etc/passwd",
            target="compile",
        )
    assert exc_info.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.parametrize(
    "frame_body",
    [
        # missing accepted
        {"type": "submit_job_ack", "job_id": "j-1"},
        # accepted is wrong type
        {"type": "submit_job_ack", "job_id": "j-1", "accepted": "yes"},
        # job_id missing
        {"type": "submit_job_ack", "accepted": True},
    ],
    ids=["missing-accepted", "non-bool-accepted", "missing-job_id"],
)
@pytest.mark.asyncio
async def test_dispatch_submit_job_ack_drops_malformed(
    monkeypatch: pytest.MonkeyPatch,
    frame_body: dict[str, Any],
) -> None:
    """Malformed ``submit_job_ack`` is dropped without firing a future."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._submit_job_acks["j-1"] = fut
    client._dispatch_submit_job_ack(frame_body)
    assert not fut.done()


@pytest.mark.asyncio
async def test_dispatch_submit_job_ack_drops_with_no_pending_future() -> None:
    """An ack frame with no matching future is dropped silently."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    client._dispatch_submit_job_ack(
        {"type": "submit_job_ack", "job_id": "unknown", "accepted": True}
    )
    # No future got registered, no exception raised — the
    # branch is exercised; absence of a raise is the assertion.


@pytest.mark.asyncio
async def test_dispatch_submit_job_ack_drops_already_done_future() -> None:
    """An ack frame for an already-resolved future is dropped silently."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    fut.set_result({"type": "submit_job_ack", "job_id": "j-1", "accepted": True})
    client._submit_job_acks["j-1"] = fut
    client._dispatch_submit_job_ack(
        {"type": "submit_job_ack", "job_id": "j-1", "accepted": False, "reason": "late"}
    )
    # First result wins; the second ack does not raise InvalidStateError.
    assert fut.result()["accepted"] is True


@pytest.mark.asyncio
async def test_dispatch_job_output_drops_malformed_frame() -> None:
    """Missing required field on job_output is dropped silently."""
    bus = EventBus()
    captured = capture_events(bus, EventType.OFFLOADER_JOB_OUTPUT)
    client = PeerLinkClient(
        receiver_hostname="receiver.local",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    # Missing "line" field
    client._dispatch_job_output({"type": "job_output", "job_id": "j-1", "stream": "stdout"})
    assert len(captured) == 0


@pytest.mark.asyncio
async def test_dispatch_job_output_drops_invalid_stream_literal() -> None:
    """A stream value outside ``{stdout, stderr}`` is dropped."""
    bus = EventBus()
    captured = capture_events(bus, EventType.OFFLOADER_JOB_OUTPUT)
    client = PeerLinkClient(
        receiver_hostname="receiver.local",
        receiver_port=6055,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=b"\x00" * 32,
        pin_sha256="a" * 64,
        receiver_label="test-receiver",
        bus=bus,
    )
    client._dispatch_job_output(
        {"type": "job_output", "job_id": "j-1", "stream": "weird", "line": "x\n"}
    )
    assert len(captured) == 0


@pytest.mark.asyncio
async def test_controller_submit_job_yaml_invalid_maps_to_invalid_args(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BundleBuildError`` from ``build_yaml_bundle`` lands as INVALID_ARGS."""
    from esphome_device_builder.helpers.config_bundle import (  # noqa: PLC0415
        BundleBuildError,
    )

    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    _seed_open_peer_link_client(offloader, pairing)

    async def _stub_build_bundle(_path: Path) -> bytes:
        raise BundleBuildError("subprocess exited 1", output="schema validation failed")

    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_bundle.build_yaml_bundle",
        _stub_build_bundle,
    )

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.submit_job(
                pin_sha256=pairing.pin_sha256,
                configuration="kitchen.yaml",
                target="compile",
            )
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_controller_submit_job_missing_yaml_maps_to_not_found(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FileNotFoundError`` from ``build_yaml_bundle`` lands as NOT_FOUND."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    _seed_open_peer_link_client(offloader, pairing)

    async def _stub_build_bundle(_path: Path) -> bytes:
        raise FileNotFoundError("yaml gone")

    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_bundle.build_yaml_bundle",
        _stub_build_bundle,
    )

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.submit_job(
                pin_sha256=pairing.pin_sha256,
                configuration="kitchen.yaml",
                target="compile",
            )
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_controller_submit_job_orphaned_client_raises_precondition_failed(
    offloader_controller_dir: Path,
) -> None:
    """A handle whose task is .done() (orphaned) → PRECONDITION_FAILED with reason."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    # Build a handle whose task is already finished (orphaned).
    bus = MagicMock()
    client = PeerLinkClient(
        receiver_hostname=pairing.receiver_hostname,
        receiver_port=pairing.receiver_port,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=pairing.static_x25519_pub,
        pin_sha256=pairing.pin_sha256,
        receiver_label=pairing.label,
        bus=bus,
    )

    async def _exit_immediately() -> None:
        return

    finished_task = asyncio.create_task(_exit_immediately())
    await asyncio.sleep(0)
    assert finished_task.done()
    offloader._peer_link_clients[pairing.pin_sha256] = rb._PeerLinkClientHandle(
        client=client, task=finished_task
    )

    with pytest.raises(CommandError) as exc_info:
        await offloader.submit_job(
            pin_sha256=pairing.pin_sha256,
            configuration="kitchen.yaml",
            target="compile",
        )
    assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED
    assert "orphaned" in exc_info.value.message


@pytest.mark.asyncio
async def test_controller_submit_job_session_closed_branch_in_lookup(
    offloader_controller_dir: Path,
) -> None:
    """A handle whose client has no live session lands as PRECONDITION_FAILED.

    Distinct from the no-handle branch (covered by
    ``test_controller_submit_job_no_session_raises_precondition_failed``)
    and the orphaned-task branch (covered by
    ``test_controller_submit_job_orphaned_client_raises_precondition_failed``).
    Exercises the third "not ready" sub-branch — handle
    present, task alive, but the peer-link is mid-reconnect /
    receiver offline so ``is_session_open`` is False.
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    bus = MagicMock()
    client = PeerLinkClient(
        receiver_hostname=pairing.receiver_hostname,
        receiver_port=pairing.receiver_port,
        identity_priv=secrets.token_bytes(32),
        dashboard_id="alpha",
        pinned_static_x25519_pub=pairing.static_x25519_pub,
        pin_sha256=pairing.pin_sha256,
        receiver_label=pairing.label,
        bus=bus,
    )
    # Don't set ``_active_channel`` — ``is_session_open`` returns False.
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    task: asyncio.Task[None] = asyncio.create_task(_park())
    offloader._peer_link_clients[pairing.pin_sha256] = rb._PeerLinkClientHandle(
        client=client, task=task
    )

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.submit_job(
                pin_sha256=pairing.pin_sha256,
                configuration="kitchen.yaml",
                target="compile",
            )
        assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED
        assert "session not connected" in exc_info.value.message
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_controller_submit_job_no_session_during_send_maps_to_precondition_failed(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PeerLinkNoSessionError`` raised mid-send → CommandError(PRECONDITION_FAILED).

    Race-window coverage: the lookup succeeded
    (``is_session_open`` was True at lookup time), but by the
    time ``client.submit_job`` actually drives the send the
    session has gone away. The WS layer maps the exception
    class to PRECONDITION_FAILED rather than UNAVAILABLE
    because the operator's resolution is the same as for the
    lookup-time branch (wait for reconnect, retry).
    """
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    (Path(offloader._db.settings.config_dir) / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n", encoding="utf-8"
    )
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_build_bundle(_path: Path) -> bytes:
        return b"bundle-bytes"

    async def _stub_submit_job(**kwargs: Any) -> dict[str, Any]:
        raise PeerLinkNoSessionError("session lost between lookup and send")

    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_bundle.build_yaml_bundle",
        _stub_build_bundle,
    )
    monkeypatch.setattr(client, "submit_job", _stub_submit_job)

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.submit_job(
                pin_sha256=pairing.pin_sha256,
                configuration="kitchen.yaml",
                target="compile",
            )
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED


# ---------------------------------------------------------------------------
# Phase 5c-3: offloader-side in-flight remote-job cache
# ---------------------------------------------------------------------------


def _fire_offloader_job_state(
    offloader: RemoteBuildController,
    *,
    pin_sha256: str = "a" * 64,
    receiver_hostname: str = "rcv.local",
    receiver_port: int = 6055,
    job_id: str,
    status: str,
    error_message: str = "",
) -> None:
    """Invoke the cache listener directly with a synthetic event payload.

    The listener is wired on the real bus in :meth:`start`,
    but startup also depends on zeroconf availability. Calling
    the sync listener directly is the established pattern in
    this file (see
    ``test_offloader_peer_link_event_listeners_update_open_set``)
    — keeps the test focused on the cache contract without
    standing up the full controller.
    """
    data: OffloaderJobStateChangedData = {
        "receiver_hostname": receiver_hostname,
        "receiver_port": receiver_port,
        "pin_sha256": pin_sha256,
        "job_id": job_id,
        "status": cast(Any, status),
        "error_message": error_message,
    }
    offloader._on_offloader_job_state_changed(MagicMock(data=data))


@pytest.mark.asyncio
async def test_offloader_remote_jobs_cache_seeded_on_running_event(
    offloader_controller_dir: Path,
) -> None:
    """A ``running`` event populates the cache so late tabs see the in-flight job."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    _fire_offloader_job_state(offloader, job_id="j-1", status="running")
    snapshot = offloader.offloader_remote_jobs_snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["job_id"] == "j-1"
    assert snapshot[0]["status"] == "running"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
@pytest.mark.asyncio
async def test_offloader_remote_jobs_cache_drops_on_terminal_event(
    offloader_controller_dir: Path,
    status: str,
) -> None:
    """A terminal ``status`` drops the cache entry."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    _fire_offloader_job_state(offloader, job_id="j-1", status="running")
    assert len(offloader.offloader_remote_jobs_snapshot()) == 1
    _fire_offloader_job_state(offloader, job_id="j-1", status=status)
    assert offloader.offloader_remote_jobs_snapshot() == []


@pytest.mark.asyncio
async def test_offloader_remote_jobs_cache_cleared_on_unpair(
    offloader_controller_dir: Path,
) -> None:
    """Unpairing a peer drops in-flight job entries for that pin."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    _fire_offloader_job_state(
        offloader, pin_sha256=pairing.pin_sha256, job_id="j-pin-a", status="running"
    )
    # And a job under a different pin — should NOT be dropped.
    _fire_offloader_job_state(offloader, pin_sha256="b" * 64, job_id="j-pin-b", status="running")
    assert len(offloader.offloader_remote_jobs_snapshot()) == 2

    await offloader.unpair(pin_sha256=pairing.pin_sha256)
    remaining = offloader.offloader_remote_jobs_snapshot()
    assert len(remaining) == 1
    assert remaining[0]["job_id"] == "j-pin-b"


# ---------------------------------------------------------------------------
# Phase 5d: cancel_job (offloader → receiver cooperative cancel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_link_client_cancel_job_sends_frame_through_channel() -> None:
    """:meth:`PeerLinkClient.cancel_job` writes a ``cancel_job`` frame on the wire."""
    initiator, responder = _build_handshake_pair()
    captured: list[dict[str, Any]] = []

    class _RecordingWs(_ParkingWs):
        async def send_bytes(self, data: bytes) -> None:
            payload = _json.loads(responder.decrypt(data))
            captured.append(payload)

    closed_event = asyncio.Event()
    ws = _RecordingWs(closed_event)
    client = _make_offloader_client(EventBus())
    client._active_channel = PeerLinkChannel(noise=initiator, ws=ws, log_label="127.0.0.1:6055")

    sent = await client.cancel_job(job_id="j-1")
    assert sent is True
    assert captured == [{"type": "cancel_job", "job_id": "j-1"}]


@pytest.mark.asyncio
async def test_peer_link_client_cancel_job_raises_when_session_closed() -> None:
    """``cancel_job`` without a live session raises :class:`PeerLinkNoSessionError`."""
    client = _make_offloader_client(EventBus())
    assert not client.is_session_open
    with pytest.raises(PeerLinkNoSessionError):
        await client.cancel_job(job_id="j-1")


@pytest.mark.asyncio
async def test_controller_cancel_job_dispatches_via_client(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: validates input, looks up the client, calls cancel_job."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[pairing.pin_sha256] = pairing
    client = _seed_open_peer_link_client(offloader, pairing)

    captured_kwargs: dict[str, Any] = {}

    async def _stub_cancel(*, job_id: str) -> bool:
        captured_kwargs["job_id"] = job_id
        return True

    monkeypatch.setattr(client, "cancel_job", _stub_cancel)

    try:
        result = await offloader.cancel_job(pin_sha256=pairing.pin_sha256, job_id="j-1")
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert result == {"sent": True}
    assert captured_kwargs == {"job_id": "j-1"}


@pytest.mark.asyncio
async def test_controller_cancel_job_empty_job_id_raises_invalid_args(
    offloader_controller_dir: Path,
) -> None:
    """An empty ``job_id`` arg gets rejected upfront."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    with pytest.raises(CommandError) as exc_info:
        await offloader.cancel_job(pin_sha256="a" * 64, job_id="")
    assert exc_info.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_controller_cancel_job_unknown_pairing_raises_not_found(
    offloader_controller_dir: Path,
) -> None:
    """No pairing under the given pin → NOT_FOUND."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    with pytest.raises(CommandError) as exc_info:
        await offloader.cancel_job(pin_sha256="b" * 64, job_id="j-1")
    assert exc_info.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_controller_cancel_job_no_session_raises_precondition_failed(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PeerLinkNoSessionError`` from the client → PRECONDITION_FAILED."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_cancel(**_kwargs: Any) -> bool:
        raise PeerLinkNoSessionError("session vanished between lookup and send")

    monkeypatch.setattr(client, "cancel_job", _stub_cancel)

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.cancel_job(pin_sha256=pairing.pin_sha256, job_id="j-1")
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED


# ---------------------------------------------------------------------------
# PeerLinkClient.download_artifacts — flow tests (issue #106 phase 6a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_artifacts_raises_no_session_error_when_session_closed() -> None:
    """:meth:`download_artifacts` without a live session raises :class:`PeerLinkNoSessionError`."""
    client = _make_offloader_client(EventBus())
    assert not client.is_session_open
    with pytest.raises(PeerLinkNoSessionError):
        await client.download_artifacts(job_id="j-1")


@pytest.mark.asyncio
async def test_download_artifacts_rejects_duplicate_job_id_on_same_session() -> None:
    """A second concurrent download on the same job_id raises :class:`PeerLinkNoSessionError`."""
    client = _make_offloader_client(EventBus())
    parked: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._artifacts_downloads["already-running"] = _DownloadArtifactsState(future=parked)
    # Spoof an open channel so the no-session check passes.
    client._active_channel = MagicMock()
    with pytest.raises(PeerLinkNoSessionError, match="duplicate download"):
        await client.download_artifacts(job_id="already-running")


@pytest.mark.asyncio
async def test_dispatch_artifacts_resolves_future_with_tarball_and_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start → chunk → end{accepted: true} resolves the future with bytes + offset."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    tarball = b"TAR" * 50
    job_id = "dl-1"

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._artifacts_downloads[job_id] = _DownloadArtifactsState(future=fut)

    chunks_iter = list(chunk_bundle(tarball))
    frames: list[dict[str, Any]] = [
        {
            "type": "artifacts_start",
            "job_id": job_id,
            "total_bytes": len(tarball),
            "num_chunks": len(chunks_iter),
            "artifacts_sha256": compute_bundle_sha256(tarball),
            "firmware_offset": "0x10000",
        },
        *(
            {
                "type": "artifacts_chunk",
                "job_id": job_id,
                "chunk_index": idx,
                "data_b64": encode_chunk(raw),
                "is_last": is_last,
            }
            for idx, raw, is_last in chunks_iter
        ),
        {"type": "artifacts_end", "job_id": job_id, "accepted": True},
    ]

    async with _drive_session_with_frames(client, monkeypatch, frames):
        result = await asyncio.wait_for(fut, timeout=2.0)

    assert isinstance(result, DownloadArtifactsResult)
    assert result.tarball == tarball
    assert result.firmware_offset == "0x10000"


@pytest.mark.asyncio
async def test_dispatch_artifacts_end_rejected_resolves_future_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``artifacts_end{accepted: false, reason}`` resolves the future with that reason."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    job_id = "dl-rejected"
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._artifacts_downloads[job_id] = _DownloadArtifactsState(future=fut)
    frame = {
        "type": "artifacts_end",
        "job_id": job_id,
        "accepted": False,
        "reason": "build_dir_missing",
    }
    async with _drive_session_with_frames(client, monkeypatch, [frame]):
        with pytest.raises(DownloadArtifactsError) as exc_info:
            await asyncio.wait_for(fut, timeout=2.0)

    assert exc_info.value.reason == "build_dir_missing"


@pytest.mark.asyncio
async def test_run_session_loops_drains_pending_artifacts_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending download futures complete with ``SubmitJobSessionLostError`` on session end."""
    bus = EventBus()
    client = _make_offloader_client(bus)
    pending: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client._artifacts_downloads["abandoned"] = _DownloadArtifactsState(future=pending)

    async with _drive_session_with_frames(client, monkeypatch, []):
        await asyncio.sleep(0)

    with pytest.raises(SubmitJobSessionLostError):
        await pending


# ---------------------------------------------------------------------------
# Dispatch-handler error paths — direct unit tests against the sync dispatchers
# (no full session loop needed; they take a parsed frame dict + mutate the
# per-job state on the client).
# ---------------------------------------------------------------------------


def _seed_artifacts_state(
    client: PeerLinkClient, job_id: str
) -> tuple[asyncio.Future[Any], _DownloadArtifactsState]:
    """Pre-register a per-job download future + state, return both."""
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    state = _DownloadArtifactsState(future=fut)
    client._artifacts_downloads[job_id] = state
    return fut, state


@pytest.mark.asyncio
async def test_dispatch_artifacts_start_drops_malformed_frame() -> None:
    """A frame missing ``firmware_offset`` is logged + dropped (state untouched)."""
    client = _make_offloader_client(EventBus())
    fut, state = _seed_artifacts_state(client, "j-malformed")

    client._dispatch_artifacts_start(
        {"type": "artifacts_start", "job_id": "j-malformed"}
    )  # missing every required field except job_id

    assert state.assembler is None
    assert not fut.done()


@pytest.mark.asyncio
async def test_dispatch_artifacts_start_unknown_job_id_dropped() -> None:
    """A start frame for a job nobody asked for is logged + dropped."""
    client = _make_offloader_client(EventBus())
    # No entry in _artifacts_downloads → unknown job_id.
    client._dispatch_artifacts_start(
        {
            "type": "artifacts_start",
            "job_id": "stranger",
            "total_bytes": 16,
            "num_chunks": 1,
            "artifacts_sha256": "0" * 64,
            "firmware_offset": "0x10000",
        }
    )

    # No mutation, no future to resolve — silent drop.
    assert "stranger" not in client._artifacts_downloads


@pytest.mark.asyncio
async def test_dispatch_artifacts_start_invalid_header_resolves_future_with_error() -> None:
    """A start header that ``BundleAssembler`` rejects resolves the future with an error."""
    client = _make_offloader_client(EventBus())
    fut, state = _seed_artifacts_state(client, "j-bad-header")
    # Mismatched total_bytes vs num_chunks → BundleAssemblerError.
    client._dispatch_artifacts_start(
        {
            "type": "artifacts_start",
            "job_id": "j-bad-header",
            "total_bytes": 0,  # zero bytes but one chunk announced
            "num_chunks": 1,
            "artifacts_sha256": "0" * 64,
            "firmware_offset": "0x10000",
        }
    )

    assert state.assembler is None
    assert fut.done()
    with pytest.raises(DownloadArtifactsError) as exc_info:
        await fut
    assert exc_info.value.reason == "invalid_start_header"


@pytest.mark.asyncio
async def test_dispatch_artifacts_chunk_drops_malformed_frame() -> None:
    """A chunk frame missing ``data_b64`` is logged + dropped without resolving."""
    client = _make_offloader_client(EventBus())
    fut, _state = _seed_artifacts_state(client, "j-bad-chunk")

    client._dispatch_artifacts_chunk(
        {"type": "artifacts_chunk", "job_id": "j-bad-chunk"}  # missing fields
    )

    assert not fut.done()


@pytest.mark.asyncio
async def test_dispatch_artifacts_chunk_drops_when_no_assembler_yet() -> None:
    """A chunk frame arriving before ``artifacts_start`` is logged + dropped."""
    client = _make_offloader_client(EventBus())
    fut, state = _seed_artifacts_state(client, "j-no-assembler")
    assert state.assembler is None

    client._dispatch_artifacts_chunk(
        {
            "type": "artifacts_chunk",
            "job_id": "j-no-assembler",
            "chunk_index": 0,
            "data_b64": "AAAA",
            "is_last": True,
        }
    )

    assert not fut.done()


@pytest.mark.asyncio
async def test_dispatch_artifacts_chunk_assembler_error_resolves_future() -> None:
    """A chunk that drives the assembler past its bounds resolves the future with an error."""
    payload = b"hello"
    sha256_hex = compute_bundle_sha256(payload)
    client = _make_offloader_client(EventBus())
    fut, state = _seed_artifacts_state(client, "j-bad-chunk-feed")
    client._dispatch_artifacts_start(
        {
            "type": "artifacts_start",
            "job_id": "j-bad-chunk-feed",
            "total_bytes": len(payload),
            "num_chunks": 1,
            "artifacts_sha256": sha256_hex,
            "firmware_offset": "0x10000",
        }
    )
    assert state.assembler is not None
    # Out-of-range chunk_index → BundleAssemblerError.
    client._dispatch_artifacts_chunk(
        {
            "type": "artifacts_chunk",
            "job_id": "j-bad-chunk-feed",
            "chunk_index": 99,  # only chunk 0 expected
            "data_b64": encode_chunk(payload),
            "is_last": True,
        }
    )

    assert fut.done()
    with pytest.raises(DownloadArtifactsError):
        await fut


@pytest.mark.asyncio
async def test_dispatch_artifacts_end_drops_malformed_frame() -> None:
    """An ``artifacts_end`` missing ``accepted`` is logged + dropped."""
    client = _make_offloader_client(EventBus())
    fut, _state = _seed_artifacts_state(client, "j-bad-end")

    client._dispatch_artifacts_end({"type": "artifacts_end", "job_id": "j-bad-end"})

    assert not fut.done()


@pytest.mark.asyncio
async def test_dispatch_artifacts_end_skips_already_resolved_future() -> None:
    """A late ``artifacts_end`` for a future that's already done is a no-op."""
    client = _make_offloader_client(EventBus())
    fut, _state = _seed_artifacts_state(client, "j-already-done")
    fut.set_exception(SubmitJobSessionLostError("session lost first"))

    # No raise, no second set_*: the dispatcher returns silently.
    client._dispatch_artifacts_end(
        {"type": "artifacts_end", "job_id": "j-already-done", "accepted": True}
    )


@pytest.mark.asyncio
async def test_dispatch_artifacts_end_accept_without_start_resolves_with_missing_start() -> None:
    """``artifacts_end{accepted:true}`` with no prior start fires ``missing_start``."""
    client = _make_offloader_client(EventBus())
    fut, state = _seed_artifacts_state(client, "j-no-start")
    assert state.assembler is None

    client._dispatch_artifacts_end(
        {"type": "artifacts_end", "job_id": "j-no-start", "accepted": True}
    )

    with pytest.raises(DownloadArtifactsError) as exc_info:
        await fut
    assert exc_info.value.reason == "missing_start"


@pytest.mark.asyncio
async def test_download_artifacts_returns_result_on_full_round_trip() -> None:
    """End-to-end :meth:`download_artifacts` returns the assembled :class:`DownloadArtifactsResult`.

    Spoofs ``_active_channel`` with a stub whose ``send_frame``
    schedules the matching ``artifacts_*`` response by calling
    the dispatchers inline. Exercises the public method body
    (registers the future, sends the request, awaits the
    result, pops the slot) end-to-end without standing up a
    full Noise session.
    """
    payload = b"TARBALL-BYTES" * 8
    sha256_hex = compute_bundle_sha256(payload)
    client = _make_offloader_client(EventBus())

    async def _send_frame(frame: dict[str, Any]) -> bool:
        # Fan in the receiver-side response inline. The
        # dispatchers run on the same event loop so a single
        # await yields back to the caller after each call.
        client._dispatch_artifacts_start(
            {
                "type": "artifacts_start",
                "job_id": frame["job_id"],
                "total_bytes": len(payload),
                "num_chunks": 1,
                "artifacts_sha256": sha256_hex,
                "firmware_offset": "0x10000",
            }
        )
        client._dispatch_artifacts_chunk(
            {
                "type": "artifacts_chunk",
                "job_id": frame["job_id"],
                "chunk_index": 0,
                "data_b64": encode_chunk(payload),
                "is_last": True,
            }
        )
        client._dispatch_artifacts_end(
            {"type": "artifacts_end", "job_id": frame["job_id"], "accepted": True}
        )
        return True

    channel = MagicMock()
    channel.send_frame = _send_frame
    client._active_channel = channel

    result = await client.download_artifacts(job_id="happy-path")

    assert isinstance(result, DownloadArtifactsResult)
    assert result.tarball == payload
    assert result.firmware_offset == "0x10000"
    # Per-job slot is freed on return.
    assert "happy-path" not in client._artifacts_downloads


@pytest.mark.asyncio
async def test_download_artifacts_send_failure_raises_session_lost() -> None:
    """``send_frame`` returning ``False`` mid-request raises :class:`SubmitJobSessionLostError`."""
    client = _make_offloader_client(EventBus())

    async def _send_frame_fails(_frame: dict[str, Any]) -> bool:
        return False  # Noise encrypt / WS send failed at this tick

    channel = MagicMock()
    channel.send_frame = _send_frame_fails
    client._active_channel = channel

    with pytest.raises(SubmitJobSessionLostError, match="request send failed"):
        await client.download_artifacts(job_id="lost-session")
    # Per-job slot is freed even on the failure path.
    assert "lost-session" not in client._artifacts_downloads


@pytest.mark.asyncio
async def test_dispatch_artifacts_end_finalise_failure_resolves_with_error() -> None:
    """A SHA mismatch at finalise resolves the future with the assembler's error code."""
    payload = b"hello world"
    correct_sha = compute_bundle_sha256(payload)
    wrong_sha = "f" * 64
    assert correct_sha != wrong_sha
    client = _make_offloader_client(EventBus())
    fut, state = _seed_artifacts_state(client, "j-sha-mismatch")
    client._dispatch_artifacts_start(
        {
            "type": "artifacts_start",
            "job_id": "j-sha-mismatch",
            "total_bytes": len(payload),
            "num_chunks": 1,
            "artifacts_sha256": wrong_sha,  # the bytes won't match this
            "firmware_offset": "0x10000",
        }
    )
    # Chunk delivers correct bytes; the wrong header sha will trip finalise.
    raw_chunks = list(chunk_bundle(payload))
    for chunk_index, raw, is_last in raw_chunks:
        client._dispatch_artifacts_chunk(
            {
                "type": "artifacts_chunk",
                "job_id": "j-sha-mismatch",
                "chunk_index": chunk_index,
                "data_b64": encode_chunk(raw),
                "is_last": is_last,
            }
        )
    assert state.assembler is not None
    client._dispatch_artifacts_end(
        {"type": "artifacts_end", "job_id": "j-sha-mismatch", "accepted": True}
    )

    with pytest.raises(DownloadArtifactsError):
        await fut


# ---------------------------------------------------------------------------
# remote_build/download_artifacts WS command (issue #106 phase 6a)
# ---------------------------------------------------------------------------


def _make_test_tarball(*, idedata_extras: list[dict[str, str]] | None = None) -> bytes:
    """Build a minimal artifacts tarball matching the receiver-side packer's layout."""
    idedata_payload = {"extra": {"flash_images": idedata_extras or []}}
    idedata_bytes = _json.dumps(idedata_payload)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="idedata.json")
        info.size = len(idedata_bytes)
        tar.addfile(info, io.BytesIO(idedata_bytes))
        firmware = b"FIRMWARE-BYTES"
        info = tarfile.TarInfo(name="firmware.bin")
        info.size = len(firmware)
        tar.addfile(info, io.BytesIO(firmware))
        for entry in idedata_extras or []:
            name = Path(entry["path"]).name
            payload = name.encode("ascii")
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_controller_download_artifacts_returns_unpacked_response(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: drives round-trip, unpacks tar.gz, returns the structured response."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(
        receiver_hostname="rcv.local",
        receiver_port=6055,
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[pairing.pin_sha256] = pairing
    client = _seed_open_peer_link_client(offloader, pairing)

    extras = [{"path": "/build/.pioenvs/x/bootloader.bin", "offset": "0x1000"}]
    tarball = _make_test_tarball(idedata_extras=extras)
    captured: dict[str, Any] = {}

    async def _stub_download(*, job_id: str) -> DownloadArtifactsResult:
        captured["job_id"] = job_id
        return DownloadArtifactsResult(tarball=tarball, firmware_offset="0x10000")

    monkeypatch.setattr(client, "download_artifacts", _stub_download)

    try:
        result = await offloader.download_artifacts(
            pin_sha256=pairing.pin_sha256,
            job_id="job-42",
        )
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )

    assert captured == {"job_id": "job-42"}
    assert result["job_id"] == "job-42"
    image_names = [image["name"] for image in result["images"]]
    assert image_names == ["firmware.bin", "bootloader.bin"]
    assert result["images"][0]["offset"] == "0x10000"
    assert result["images"][1]["offset"] == "0x1000"
    # idedata path got rewritten from receiver-absolute to basename.
    assert result["idedata"]["extra"]["flash_images"][0]["path"] == "bootloader.bin"


@pytest.mark.asyncio
async def test_controller_download_artifacts_empty_job_id_raises_invalid_args(
    offloader_controller_dir: Path,
) -> None:
    """An empty ``job_id`` arg short-circuits with ``INVALID_ARGS``."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    with pytest.raises(CommandError) as exc_info:
        await offloader.download_artifacts(pin_sha256="a" * 64, job_id="")
    assert exc_info.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_controller_download_artifacts_unknown_pairing_raises_not_found(
    offloader_controller_dir: Path,
) -> None:
    """No pairing under the given pin → ``NOT_FOUND``."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    with pytest.raises(CommandError) as exc_info:
        await offloader.download_artifacts(pin_sha256="b" * 64, job_id="j-1")
    assert exc_info.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_controller_download_artifacts_no_session_raises_precondition_failed(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PeerLinkNoSessionError`` from the client → ``PRECONDITION_FAILED``."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_download(**_kwargs: Any) -> DownloadArtifactsResult:
        raise PeerLinkNoSessionError("session vanished")

    monkeypatch.setattr(client, "download_artifacts", _stub_download)

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.download_artifacts(pin_sha256=pairing.pin_sha256, job_id="j-1")
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED


@pytest.mark.asyncio
async def test_controller_download_artifacts_session_lost_maps_to_unavailable(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SubmitJobSessionLostError`` mid-download → ``UNAVAILABLE``."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_download(**_kwargs: Any) -> DownloadArtifactsResult:
        raise SubmitJobSessionLostError("session ended mid-download")

    monkeypatch.setattr(client, "download_artifacts", _stub_download)

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.download_artifacts(pin_sha256=pairing.pin_sha256, job_id="j-1")
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.UNAVAILABLE


@pytest.mark.parametrize(
    "reason,expected_code",
    [
        ("unknown_job", ErrorCode.NOT_FOUND),
        ("build_dir_missing", ErrorCode.NOT_FOUND),
        ("job_not_completed", ErrorCode.PRECONDITION_FAILED),
        ("duplicate_download", ErrorCode.PRECONDITION_FAILED),
        ("pack_failed", ErrorCode.UNAVAILABLE),
        ("entirely_unknown_reason", ErrorCode.UNAVAILABLE),
    ],
)
@pytest.mark.asyncio
async def test_controller_download_artifacts_maps_receiver_reasons(
    reason: str,
    expected_code: ErrorCode,
    offloader_controller_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receiver-reported ``reason`` strings map to the matching ``CommandError`` code."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_download(**_kwargs: Any) -> DownloadArtifactsResult:
        raise DownloadArtifactsError(f"receiver rejected ({reason})", reason=reason)

    monkeypatch.setattr(client, "download_artifacts", _stub_download)

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.download_artifacts(pin_sha256=pairing.pin_sha256, job_id="j-1")
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_controller_download_artifacts_malformed_tarball_maps_to_invalid_args(
    offloader_controller_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tarball without ``idedata.json`` surfaces as ``INVALID_ARGS`` from the unpacker."""
    offloader = _make_offloader_controller(config_dir=offloader_controller_dir)
    offloader._db.bus = MagicMock()
    pairing = _stub_pairing(receiver_hostname="rcv.local", status=PeerStatus.APPROVED)
    offloader._pairings[pairing.pin_sha256] = pairing
    client = _seed_open_peer_link_client(offloader, pairing)

    async def _stub_download(**_kwargs: Any) -> DownloadArtifactsResult:
        return DownloadArtifactsResult(tarball=b"not a tarball at all", firmware_offset="0x0")

    monkeypatch.setattr(client, "download_artifacts", _stub_download)

    try:
        with pytest.raises(CommandError) as exc_info:
            await offloader.download_artifacts(pin_sha256=pairing.pin_sha256, job_id="j-1")
    finally:
        offloader._peer_link_clients[pairing.pin_sha256].task.cancel()
        await asyncio.gather(
            offloader._peer_link_clients[pairing.pin_sha256].task,
            return_exceptions=True,
        )
    assert exc_info.value.code == ErrorCode.INVALID_ARGS
