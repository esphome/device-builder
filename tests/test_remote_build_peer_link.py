"""
Tests for the peer-link Noise WS handler.

Two layers of coverage:

* Pure intent-dispatch tests (``_dispatch_intent``): each intent
  routes to the right controller method with the right args; the
  pairing-window gate fires for ``pair_request`` only.
* End-to-end Noise round-trips (``aiohttp.test_utils``): an
  initiator-side ``PeerLinkNoiseSession`` connects to a tiny test
  app wired with ``make_peer_link_handler``, drives the 3 XX
  messages, and decrypts the post-handshake transport frame
  carrying ``intent_response``. Verifies the wire shape end-to-end
  for ``preview`` / ``pair_request`` (open + closed window) /
  ``pair_status`` / ``peer_link``.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from esphome_device_builder.controllers.config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build_peer_link import (
    PEER_LINK_PATH,
    _dispatch_intent,
    make_peer_link_handler,
)
from esphome_device_builder.helpers import json as _json
from esphome_device_builder.helpers.peer_link_identity import (
    get_or_create_peer_link_identity,
)
from esphome_device_builder.helpers.peer_link_noise import (
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from esphome_device_builder.models import (
    IntentResponse,
    PeerLinkIntent,
    PeerStatus,
    StoredPeer,
)


def _make_controller(*, config_dir: Any = None) -> RemoteBuildController:
    db = MagicMock()
    db.devices = MagicMock()
    db.devices.zeroconf = None
    db._dashboard_advertiser = None
    db.settings = MagicMock()
    db.settings.config_dir = config_dir
    return RemoteBuildController(db)


async def _seed_peer(config_dir: Path, peer: StoredPeer) -> None:
    loop = asyncio.get_running_loop()

    def _write() -> None:
        with remote_build_settings_transaction(config_dir) as settings:
            settings.peers.append(peer)

    await loop.run_in_executor(None, _write)


# ---------------------------------------------------------------------------
# Pure dispatch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_preview_returns_ok(tmp_path: Path) -> None:
    """``intent="preview"`` doesn't hit the controller; just returns OK."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    response = await _dispatch_intent(
        controller=controller,
        intent=PeerLinkIntent.PREVIEW,
        dashboard_id="alpha",
        label="alpha",
        pin_sha256="pin",
        static_x25519_pub=b"\x00" * 32,
        peer_ip="192.168.1.10",
    )

    assert response is IntentResponse.OK
    controller._db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_pair_request_open_window_creates_pending(tmp_path: Path) -> None:
    """``intent="pair_request"`` while window open creates the row + fires event."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._db.bus.fire.reset_mock()

    pubkey = b"\xaa" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    response = await _dispatch_intent(
        controller=controller,
        intent=PeerLinkIntent.PAIR_REQUEST,
        dashboard_id="alpha",
        label="alpha",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        peer_ip="192.168.1.10",
    )

    assert response is IntentResponse.PENDING
    fire = controller._db.bus.fire
    fire.assert_called_once()
    _, payload = fire.call_args.args
    assert payload["dashboard_id"] == "alpha"
    assert payload["pin_sha256"] == pin
    await controller.stop()


@pytest.mark.asyncio
async def test_dispatch_pair_request_closed_window_returns_no_pairing_window(
    tmp_path: Path,
) -> None:
    """Closed window short-circuits before any controller mutation."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    response = await _dispatch_intent(
        controller=controller,
        intent=PeerLinkIntent.PAIR_REQUEST,
        dashboard_id="alpha",
        label="alpha",
        pin_sha256="pin",
        static_x25519_pub=b"\x00" * 32,
        peer_ip="192.168.1.10",
    )

    assert response is IntentResponse.NO_PAIRING_WINDOW
    controller._db.bus.fire.assert_not_called()
    # No row was created since the window gate fired first.

    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    assert settings.peers == []


@pytest.mark.asyncio
async def test_dispatch_peer_link_approved_returns_ok(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xbb" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    await _seed_peer(
        tmp_path,
        StoredPeer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            label="alpha",
            paired_at=1.0,
            status=PeerStatus.APPROVED,
        ),
    )

    response = await _dispatch_intent(
        controller=controller,
        intent=PeerLinkIntent.PEER_LINK,
        dashboard_id="alpha",
        label="",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        peer_ip="192.168.1.10",
    )

    assert response is IntentResponse.OK


@pytest.mark.asyncio
async def test_dispatch_pair_request_empty_dashboard_id_returns_rejected(tmp_path: Path) -> None:
    """
    pair_request with no dashboard_id is REJECTED before any controller mutation.

    The dispatcher refuses identity-bearing intents whose
    ``dashboard_id`` is missing or empty, so an offloader that
    sends an empty / non-string field can't create a nonsense
    StoredPeer row keyed on ``""``.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._db.bus.fire.reset_mock()

    response = await _dispatch_intent(
        controller=controller,
        intent=PeerLinkIntent.PAIR_REQUEST,
        dashboard_id="",  # empty — should fail the gate
        label="alpha",
        pin_sha256="pin",
        static_x25519_pub=b"\x00" * 32,
        peer_ip="192.168.1.10",
    )

    assert response is IntentResponse.REJECTED
    controller._db.bus.fire.assert_not_called()
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    assert settings.peers == []
    await controller.stop()


@pytest.mark.asyncio
async def test_dispatch_pair_request_malformed_dashboard_id_returns_rejected(
    tmp_path: Path,
) -> None:
    """
    pair_request with a dashboard_id that fails the regex/length check is REJECTED.

    The dispatcher uses the same ``DASHBOARD_ID_PATTERN`` /
    ``DASHBOARD_ID_MAX_CHARS`` contract that the WS-command path
    enforces, so an offloader sending ``"has spaces!"`` or a
    65-char string can't create a row that the WS-command path
    would later refuse to operate on.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._db.bus.fire.reset_mock()

    # Spaces aren't in the base64url alphabet.
    response = await _dispatch_intent(
        controller=controller,
        intent=PeerLinkIntent.PAIR_REQUEST,
        dashboard_id="has spaces!",
        label="alpha",
        pin_sha256="pin",
        static_x25519_pub=b"\x00" * 32,
        peer_ip="192.168.1.10",
    )

    assert response is IntentResponse.REJECTED
    controller._db.bus.fire.assert_not_called()
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    assert settings.peers == []
    await controller.stop()


@pytest.mark.asyncio
async def test_dispatch_pair_status_pending_returns_pending(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xcc" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    await _seed_peer(
        tmp_path,
        StoredPeer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            label="alpha",
            paired_at=1.0,
            status=PeerStatus.PENDING,
        ),
    )

    response = await _dispatch_intent(
        controller=controller,
        intent=PeerLinkIntent.PAIR_STATUS,
        dashboard_id="alpha",
        label="",
        pin_sha256=pin,
        static_x25519_pub=pubkey,
        peer_ip="192.168.1.10",
    )

    assert response is IntentResponse.PENDING


# ---------------------------------------------------------------------------
# End-to-end Noise round-trips via aiohttp test client
# ---------------------------------------------------------------------------


@pytest.fixture
async def peer_link_app(
    tmp_path: Path,
) -> AsyncGenerator[tuple[TestClient, RemoteBuildController, bytes], None]:
    """
    Spin up a minimal aiohttp app with the peer-link route bound.

    Returns ``(client, controller, receiver_static_pub)``: the test
    client to drive the WS, the controller backing the handler, and
    the receiver's X25519 pubkey so initiator-side tests can pin the
    expected ``remote_static_pub`` from the handshake transcript.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    # Pre-create the receiver's identity so the handler doesn't
    # race the test on first-call generation; capture the pubkey
    # for assertion.
    loop = asyncio.get_running_loop()
    identity = await loop.run_in_executor(None, get_or_create_peer_link_identity, tmp_path)

    app = web.Application()
    handler = await make_peer_link_handler(controller, tmp_path)
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, controller, identity.public_bytes
    finally:
        await client.close()
        await controller.stop()


@dataclass(frozen=True)
class _InitiatorRoundTrip:
    """
    Test-only return type for :func:`_drive_initiator_handshake`.

    Bundles the three values a caller asserts on after a Noise XX
    round-trip from the initiator side: the live session (for
    ``decrypt`` access and the captured ``remote_static_pub``),
    the still-encrypted post-handshake intent_response frame, and
    the initiator's 32-byte X25519 static pubkey so tests can pin
    the round-trip ("what the offloader presented is what the
    receiver stored").
    """

    session: PeerLinkNoiseSession
    intent_response_ciphertext: bytes
    initiator_static_pub: bytes


async def _drive_initiator_handshake(
    client: TestClient,
    msg1_payload: dict[str, Any],
    msg3_payload: dict[str, Any],
) -> _InitiatorRoundTrip:
    """Drive the 3 XX messages from the initiator side."""
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    session = PeerLinkNoiseSession.initiator(initiator_priv)
    ws = await client.ws_connect(PEER_LINK_PATH)
    try:
        # msg1: plaintext intent in the payload
        msg1 = session.write_handshake_message(_json.dumps(msg1_payload))
        await ws.send_bytes(msg1)
        # msg2: encrypted, empty payload
        msg2 = await ws.receive_bytes()
        session.read_handshake_message(msg2)
        # msg3: encrypted dashboard_id/label payload
        msg3 = session.write_handshake_message(_json.dumps(msg3_payload))
        await ws.send_bytes(msg3)
        # Post-handshake intent_response frame
        encrypted_response = await ws.receive_bytes()
    finally:
        await ws.close()
    return _InitiatorRoundTrip(
        session=session,
        intent_response_ciphertext=encrypted_response,
        initiator_static_pub=initiator_pub,
    )


def _decode_intent_response(session: PeerLinkNoiseSession, encrypted: bytes) -> str:
    return _json.loads(session.decrypt(encrypted))["intent_response"]


@pytest.mark.asyncio
async def test_e2e_preview_round_trip(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """``intent="preview"`` returns OK and the initiator can read the receiver's static pubkey."""
    client, _, receiver_static_pub = peer_link_app

    round_trip = await _drive_initiator_handshake(
        client,
        msg1_payload={"intent": "preview"},
        msg3_payload={},
    )

    assert (
        _decode_intent_response(round_trip.session, round_trip.intent_response_ciphertext)
        == IntentResponse.OK
    )
    # The receiver's static pubkey is what the offloader's preview
    # flow extracts to surface for OOB pin verification.
    assert round_trip.session.remote_static_pub == receiver_static_pub


@pytest.mark.asyncio
async def test_e2e_pair_request_open_window_creates_row(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """End-to-end: open window + pair_request → PENDING row + fired event + wire response."""
    client, controller, _ = peer_link_app
    await controller.set_pairing_window(open=True, client="receiver-tab")
    controller._db.bus.fire.reset_mock()

    round_trip = await _drive_initiator_handshake(
        client,
        msg1_payload={"intent": "pair_request"},
        msg3_payload={"dashboard_id": "alpha", "label": "alpha"},
    )

    assert (
        _decode_intent_response(round_trip.session, round_trip.intent_response_ciphertext)
        == IntentResponse.PENDING
    )

    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(
        None, load_remote_build_settings, controller._db.settings.config_dir
    )
    [peer] = settings.peers
    assert peer.dashboard_id == "alpha"
    assert peer.label == "alpha"
    assert peer.status == PeerStatus.PENDING
    # The receiver's controller derived the pin from the
    # handshake transcript's authenticated initiator static
    # pubkey, not from anything in msg3. Pin the round-trip:
    # what we presented is what landed on disk.
    assert peer.static_x25519_pub == round_trip.initiator_static_pub
    assert peer.pin_sha256 == pin_sha256_for_pubkey(round_trip.initiator_static_pub)


@pytest.mark.asyncio
async def test_e2e_pair_request_closed_window_returns_no_pairing_window(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """Closed window: pair_request returns NO_PAIRING_WINDOW and no row is created."""
    client, controller, _ = peer_link_app

    round_trip = await _drive_initiator_handshake(
        client,
        msg1_payload={"intent": "pair_request"},
        msg3_payload={"dashboard_id": "alpha", "label": "alpha"},
    )

    assert (
        _decode_intent_response(round_trip.session, round_trip.intent_response_ciphertext)
        == IntentResponse.NO_PAIRING_WINDOW
    )

    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(
        None, load_remote_build_settings, controller._db.settings.config_dir
    )
    assert settings.peers == []


@pytest.mark.asyncio
async def test_e2e_peer_link_approved_returns_ok(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """End-to-end: approved peer's peer_link intent gets OK."""
    client, controller, _ = peer_link_app

    # Pre-seed an APPROVED peer whose pubkey matches what the
    # initiator below will present. We need the initiator's
    # priv first so we can compute its pubkey; build the
    # session manually.
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    pin = hashlib.sha256(initiator_pub).hexdigest()
    await _seed_peer(
        controller._db.settings.config_dir,
        StoredPeer(
            dashboard_id="alpha",
            pin_sha256=pin,
            static_x25519_pub=initiator_pub,
            label="alpha",
            paired_at=1.0,
            status=PeerStatus.APPROVED,
        ),
    )

    session = PeerLinkNoiseSession.initiator(initiator_priv)
    ws = await client.ws_connect(PEER_LINK_PATH)
    try:
        msg1 = session.write_handshake_message(_json.dumps({"intent": "peer_link"}))
        await ws.send_bytes(msg1)
        msg2 = await ws.receive_bytes()
        session.read_handshake_message(msg2)
        msg3 = session.write_handshake_message(_json.dumps({"dashboard_id": "alpha"}))
        await ws.send_bytes(msg3)
        encrypted = await ws.receive_bytes()
    finally:
        await ws.close()

    assert _decode_intent_response(session, encrypted) == IntentResponse.OK


@pytest.mark.asyncio
async def test_e2e_unknown_intent_completes_handshake_then_rejects(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """Unknown intent completes the handshake before sending REJECTED in an authenticated frame."""
    client, _, _ = peer_link_app

    round_trip = await _drive_initiator_handshake(
        client,
        msg1_payload={"intent": "evil_intent"},
        msg3_payload={"dashboard_id": "alpha"},
    )

    assert (
        _decode_intent_response(round_trip.session, round_trip.intent_response_ciphertext)
        == IntentResponse.REJECTED
    )


@pytest.mark.asyncio
async def test_e2e_garbage_msg1_payload_handled_gracefully(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """A non-JSON msg1 payload is treated as unknown intent (REJECTED), not a server crash."""
    client, _, _ = peer_link_app

    initiator_priv = secrets.token_bytes(32)
    session = PeerLinkNoiseSession.initiator(initiator_priv)
    ws = await client.ws_connect(PEER_LINK_PATH)
    try:
        msg1 = session.write_handshake_message(b"not json")
        await ws.send_bytes(msg1)
        msg2 = await ws.receive_bytes()
        session.read_handshake_message(msg2)
        msg3 = session.write_handshake_message(b"")
        await ws.send_bytes(msg3)
        encrypted = await ws.receive_bytes()
    finally:
        await ws.close()

    assert _decode_intent_response(session, encrypted) == IntentResponse.REJECTED
