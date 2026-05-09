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

from esphome_device_builder.controllers import remote_build_peer_link_client
from esphome_device_builder.controllers.config import (
    load_offloader_remote_build_settings,
)
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
from esphome_device_builder.models import ErrorCode, IntentResponse, PeerLinkIntent, PeerStatus


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
    peers = await controller.list_peers()
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
    saved = await asyncio.get_running_loop().run_in_executor(
        None, load_offloader_remote_build_settings, offloader_controller_dir
    )
    assert saved.pairings == []


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
    # Persisted to disk under the offloader's _offloader_remote_build key.
    saved = await asyncio.get_running_loop().run_in_executor(
        None, load_offloader_remote_build_settings, offloader_controller_dir
    )
    assert len(saved.pairings) == 1
    assert saved.pairings[0].pin_sha256 == expected_pin


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
    saved = await asyncio.get_running_loop().run_in_executor(
        None, load_offloader_remote_build_settings, offloader_controller_dir
    )
    assert saved.pairings == []


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
