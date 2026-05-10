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
import secrets
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from esphome_device_builder.controllers import remote_build_peer_link_client
from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build_peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.controllers.remote_build_peer_link_client import (
    PeerLinkClientError,
    RequestPairResult,
    _build_ws_url,
    drive_initiator_round_trip,
    preview_pair,
    request_pair,
)
from esphome_device_builder.helpers.api import CommandError
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
    PeerLinkIntent,
    PeerStatus,
    StoredPairing,
    StoredPeer,
)


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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
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
    server, _, _ = receiver_server
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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
) -> None:
    """Request_pair against an open pairing window returns PENDING + lands a peer row."""
    server, controller, expected_pin = receiver_server
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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
) -> None:
    """Request_pair when the receiver window is closed returns NO_PAIRING_WINDOW."""
    server, _, _ = receiver_server
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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
    offloader_controller_dir: Path,
) -> None:
    """End-to-end: ``RemoteBuildController.preview_pair`` returns the receiver's pin."""
    server, _, expected_pin = receiver_server

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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
    offloader_controller_dir: Path,
) -> None:
    """End-to-end: ``RemoteBuildController.request_pair`` persists a PENDING StoredPairing."""
    server, receiver_controller, expected_pin = receiver_server
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
    assert ("127.0.0.1", server.port) in offloader._pairings


@pytest.mark.asyncio
async def test_controller_request_pair_pin_mismatch_raises_precondition_failed(
    receiver_server: tuple[TestServer, RemoteBuildController, str],
    offloader_controller_dir: Path,
) -> None:
    """User-supplied pin doesn't match the handshake → PRECONDITION_FAILED.

    A receiver-side identity rotation between preview and request would land here
    in production; the test forces the same shape by passing a wrong pin.
    """
    server, receiver_controller, _ = receiver_server
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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
    offloader_controller_dir: Path,
) -> None:
    """Receiver window closed → CommandError(NO_PAIRING_WINDOW)."""
    server, _, expected_pin = receiver_server
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
        "esphome_device_builder.controllers.remote_build.peer_link_request_pair",
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
    offloader._pairings[("rcv.local", 6055)] = pairing
    # A no-op listener task standing in for the real long-poll
    # task; the test verifies cancel + cleanup, not the wire flow.
    park = asyncio.Event()

    async def _park() -> None:
        await park.wait()

    offloader._pair_status_listeners[("rcv.local", 6055)] = asyncio.create_task(_park())
    # Settle one event-loop tick so the create_task is actually
    # scheduled before unpair tries to cancel it.
    await asyncio.sleep(0)

    result = await offloader.unpair(hostname="rcv.local", port=6055)

    assert result == {"removed": True}
    assert ("rcv.local", 6055) not in offloader._pairings
    # Listener was cancelled; let the loop run so the cancellation
    # propagates and we can drain it cleanly.
    listener = offloader._pair_status_listeners.get(("rcv.local", 6055))
    if listener is not None:
        with pytest.raises(asyncio.CancelledError):
            await listener


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
    offloader._pairings[("rcv.local", 6055)] = pairing
    offloader._pairings_store.async_delay_save(offloader._serialize_pairings, delay=0.0)
    for cb in offloader._shutdown_callbacks:
        await cb()

    result = await offloader.unpair(hostname="rcv.local", port=6055)

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

    result = await offloader.unpair(hostname="ghost.local", port=6055)

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
        label="pending-receiver",
        status=PeerStatus.PENDING,
    )
    approved = _stub_pairing(
        receiver_hostname="approved.local",
        receiver_port=6055,
        label="approved-receiver",
        status=PeerStatus.APPROVED,
    )
    offloader._pairings[("pending.local", 6055)] = pending
    offloader._pairings[("approved.local", 6055)] = approved

    rows = offloader.pairings_snapshot()

    by_host = {row.receiver_hostname: row for row in rows}
    assert by_host["pending.local"].status is PeerStatus.PENDING
    assert by_host["approved.local"].status is PeerStatus.APPROVED


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
    seeder._pairings[("seeded.local", 6055)] = seeded
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

    assert ("seeded.local", 6055) in fresh._pairings
    loaded = fresh._pairings[("seeded.local", 6055)]
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
    offloader._pairings[("rcv.local", 6055)] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.APPROVED, pin_sha256=pin
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    assert terminal is True
    # Row stays in the dict with promoted status — the unified
    # ``_pairings`` map carries both PENDING and APPROVED.
    assert offloader._pairings[("rcv.local", 6055)].status is PeerStatus.APPROVED
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
    offloader._pairings[("rcv.local", 6055)] = pairing
    # Receiver returned APPROVED but its pubkey hash doesn't match
    # what we stored; treat as peer-revoked rather than silently
    # adopting the new identity.
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.APPROVED, pin_sha256="b" * 64
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    assert terminal is True
    assert ("rcv.local", 6055) not in offloader._pairings
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
    offloader._pairings[("rcv.local", 6055)] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.REJECTED, pin_sha256="a" * 64
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    assert terminal is True
    assert ("rcv.local", 6055) not in offloader._pairings
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
    offloader._pairings[("rcv.local", 6055)] = pairing
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
    offloader._pairings[("rcv.local", 6055)] = pairing
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
    offloader._pairings[("rcv.local", 6055)] = pairing
    offloader._offloader_alerts[("rcv.local", 6055)] = {
        "kind": "peer_revoked",
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
        "receiver_label": "lab-pc",
        "fired_at": 1.0,
    }

    result = await offloader.unpair(hostname="rcv.local", port=6055)

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
    offloader._pairings[("rcv.local", 6055)] = pairing
    result = remote_build_peer_link_client.PairStatusResult(
        status=IntentResponse.OK, pin_sha256="a" * 64
    )

    terminal = await offloader._apply_pair_status_result(pairing, result)

    # Not terminal — the listener loop reconnects after a backoff.
    assert terminal is False
    # Pending entry untouched.
    assert ("rcv.local", 6055) in offloader._pairings
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
    assert ("rcv.local", 6055) not in offloader._pairings

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

    key = ("rcv.local", 6055)
    pairing = _stub_pairing(receiver_hostname="rcv.local", receiver_port=6055)
    offloader._pairings[key] = pairing
    listener = asyncio.create_task(_park())
    offloader._pair_status_listeners[key] = listener
    await asyncio.sleep(0)

    await offloader.unpair(hostname="rcv.local", port=6055)

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
    offloader._pairings[("rcv.local", 6055)] = pairing

    result = await offloader.unpair(hostname="rcv.local", port=6055)

    assert result == {"removed": True}
    fire = offloader._db.bus.fire
    fire.assert_called_once()
    event_type, payload = fire.call_args.args
    assert event_type is EventType.OFFLOADER_PAIR_STATUS_CHANGED
    assert payload == {
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
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
        "esphome_device_builder.controllers.remote_build.peer_link_request_pair",
        _fake_request_pair,
    )
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "dashboard-stub"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build._load_offloader_identities",
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
        "esphome_device_builder.controllers.remote_build.peer_link_await_pair_status",
        _fake_await_pair_status,
    )

    # Pre-seed an alert as if a prior pair-status detection had
    # registered one against this receiver.
    offloader._offloader_alerts[("rcv.local", 6055)] = {
        "kind": "pin_mismatch",
        "receiver_hostname": "rcv.local",
        "receiver_port": 6055,
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
    listener = offloader._pair_status_listeners.get(("rcv.local", 6055))
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

    result = await offloader.unpair(hostname="ghost.local", port=6055)

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
        "esphome_device_builder.controllers.remote_build.peer_link_request_pair",
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
        "esphome_device_builder.controllers.remote_build.peer_link_await_pair_status",
        _fake_await_pair_status,
    )
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "dashboard-stub"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build._load_offloader_identities",
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
    listener_v1 = offloader._pair_status_listeners[("rcv.local", 6055)]

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
    listener_v2 = offloader._pair_status_listeners[("rcv.local", 6055)]
    assert listener_v2 is not listener_v1
    with pytest.raises(asyncio.CancelledError):
        await listener_v1
    assert offloader._pairings[("rcv.local", 6055)].pin_sha256 == pin2

    # Unpair cancels listener_v2 + clears the dict.
    await offloader.unpair(hostname="rcv.local", port=6055)
    with pytest.raises(asyncio.CancelledError):
        await listener_v2
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

    key = ("rcv.local", 6055)
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
    offloader._cancel_pair_status_listener("ghost.local", 6055)


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

    key = ("rcv.local", 6055)
    old_pairing = _stub_pairing(
        receiver_hostname="rcv.local", receiver_port=6055, pin_sha256="a" * 64
    )
    offloader._pairings[key] = old_pairing
    old_listener = asyncio.create_task(_park())
    offloader._pair_status_listeners[key] = old_listener
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
        "esphome_device_builder.controllers.remote_build.peer_link_request_pair",
        _fake_request_pair,
    )

    summary = await offloader.request_pair(
        hostname="rcv.local",
        port=6055,
        pin_sha256=new_pin,
        receiver_label="rcv-label",
        offloader_label="off-label",
    )

    # Old listener got cancelled (cleared pin-drift exposure).
    with pytest.raises(asyncio.CancelledError):
        await old_listener
    # New listener spawned with the fresh pairing.
    new_listener = offloader._pair_status_listeners.get(key)
    assert new_listener is not None
    assert new_listener is not old_listener
    new_listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await new_listener
    # The dict entry now holds the new pin.
    assert offloader._pairings[key].pin_sha256 == new_pin
    assert summary.pin_sha256 == new_pin


@pytest.mark.asyncio
async def test_request_pair_already_approved_persists_to_disk(
    receiver_server: tuple[TestServer, RemoteBuildController, str],
    offloader_controller_dir: Path,
) -> None:
    """Re-pair against an already-approved row persists APPROVED, no listener spawn.

    Drives the full e2e path: first request_pair lands PENDING,
    receiver-side admin Accepts (promoting to APPROVED), then a
    second request_pair finds the receiver returning
    ``intent_response=approved`` immediately and writes the row
    to the offloader's persistent file.
    """
    server, receiver_controller, expected_pin = receiver_server
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
    offloader._cancel_pair_status_listener("127.0.0.1", server.port)

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
    assert offloader._pairings[("127.0.0.1", server.port)].status is PeerStatus.APPROVED


@pytest.mark.asyncio
async def test_lookup_peer_for_status_pending_dict_pin_mismatch_returns_rejected(
    receiver_server: tuple[TestServer, RemoteBuildController, str],
) -> None:
    """An offloader presenting a wrong pin against a PENDING dict entry → REJECTED.

    The dict-then-list lookup in ``_lookup_peer_response`` runs
    the pin check on the dict entry and returns REJECTED on
    mismatch (not PENDING) so a peer with a stale / impersonated
    pubkey can't pretend to be a legitimate pending offloader.
    """
    _, controller, _ = receiver_server
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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
    offloader_controller_dir: Path,
) -> None:
    """await_pair_status against an APPROVED receiver row returns APPROVED + receiver pin."""
    server, controller, expected_pin = receiver_server
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
    receiver_server: tuple[TestServer, RemoteBuildController, str],
) -> None:
    """await_pair_status against an unknown dashboard_id returns REJECTED."""
    server, _, _ = receiver_server
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
        "esphome_device_builder.controllers.remote_build._PAIR_STATUS_RECONNECT_BACKOFF_SECONDS",
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
    offloader._pairings[("rcv.local", 6055)] = pairing

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
        "esphome_device_builder.controllers.remote_build.peer_link_await_pair_status",
        _fake_poll,
    )
    # Stub identity load so it doesn't try to read real key files.
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "alpha"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build._load_offloader_identities",
        lambda _config_dir: (fake_identity, fake_dashboard),
    )

    await offloader._await_pair_status_flip(pairing)

    # Two poll attempts (transport error → backoff → success).
    assert calls == 2
    # Terminal branch ran: row stays in the dict but is now
    # APPROVED, and an approved event fired.
    assert offloader._pairings[("rcv.local", 6055)].status is PeerStatus.APPROVED


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
        "esphome_device_builder.controllers.remote_build._PAIR_STATUS_RECONNECT_BACKOFF_SECONDS",
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
    offloader._pairings[("rcv.local", 6055)] = pairing

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
        "esphome_device_builder.controllers.remote_build.peer_link_await_pair_status",
        _fake_poll,
    )
    fake_identity = MagicMock()
    fake_identity.private_bytes = b"\x00" * 32
    fake_dashboard = MagicMock()
    fake_dashboard.dashboard_id = "alpha"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build._load_offloader_identities",
        lambda _config_dir: (fake_identity, fake_dashboard),
    )

    await offloader._await_pair_status_flip(pairing)

    # Two poll attempts (OK → backoff → REJECTED → terminal).
    assert calls == 2
    assert ("rcv.local", 6055) not in offloader._pairings
