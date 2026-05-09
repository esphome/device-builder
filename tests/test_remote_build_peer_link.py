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
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import WSMessage, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from noise.exceptions import NoiseInvalidMessage

from esphome_device_builder.controllers.config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build_peer_link import (
    _PEER_LABEL_MAX_CHARS,
    PEER_LINK_PATH,
    _dispatch_intent,
    _DispatchInput,
    _drive_peer_link_session,
    _HandshakeStep,
    _normalize_label,
    _parse_intent,
    _parse_json,
    _read_handshake_message,
    _send_bytes_safely,
    _send_handshake_message,
    _send_response,
    make_peer_link_handler,
)
from esphome_device_builder.helpers import json as _json
from esphome_device_builder.helpers.peer_link_identity import (
    get_or_create_peer_link_identity,
)
from esphome_device_builder.helpers.peer_link_noise import (
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from esphome_device_builder.models import (
    IntentResponse,
    PeerLinkIntent,
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
        controller,
        _DispatchInput(
            intent=PeerLinkIntent.PREVIEW,
            dashboard_id="alpha",
            label="alpha",
            pin_sha256="pin",
            static_x25519_pub=b"\x00" * 32,
            peer_ip="192.168.1.10",
        ),
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
        controller,
        _DispatchInput(
            intent=PeerLinkIntent.PAIR_REQUEST,
            dashboard_id="alpha",
            label="alpha",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            peer_ip="192.168.1.10",
        ),
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
        controller,
        _DispatchInput(
            intent=PeerLinkIntent.PAIR_REQUEST,
            dashboard_id="alpha",
            label="alpha",
            pin_sha256="pin",
            static_x25519_pub=b"\x00" * 32,
            peer_ip="192.168.1.10",
        ),
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
        ),
    )

    response = await _dispatch_intent(
        controller,
        _DispatchInput(
            intent=PeerLinkIntent.PEER_LINK,
            dashboard_id="alpha",
            label="",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            peer_ip="192.168.1.10",
        ),
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
        controller,
        _DispatchInput(
            intent=PeerLinkIntent.PAIR_REQUEST,
            dashboard_id="",  # empty — should fail the gate
            label="alpha",
            pin_sha256="pin",
            static_x25519_pub=b"\x00" * 32,
            peer_ip="192.168.1.10",
        ),
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
        controller,
        _DispatchInput(
            intent=PeerLinkIntent.PAIR_REQUEST,
            dashboard_id="has spaces!",
            label="alpha",
            pin_sha256="pin",
            static_x25519_pub=b"\x00" * 32,
            peer_ip="192.168.1.10",
        ),
    )

    assert response is IntentResponse.REJECTED
    controller._db.bus.fire.assert_not_called()
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(None, load_remote_build_settings, tmp_path)
    assert settings.peers == []
    await controller.stop()


@pytest.mark.asyncio
async def test_dispatch_pair_status_unknown_after_window_close_returns_rejected(
    tmp_path: Path,
) -> None:
    """A pair_status from a peer that no longer has a row gets REJECTED.

    Concrete scenario: offloader sent a pair_request during an
    open window, receiver added a PENDING entry to its in-memory
    dict, admin closed the window before clicking Accept.
    Window-close clears the dict. The offloader's stale pair_status
    listener reconnects; with the dict cleared and the peer never
    promoted to ``settings.peers``, the lookup returns REJECTED.
    The offloader's listener treats REJECTED as peer-revoked +
    drops its local pending state — clean exit, user re-pairs
    when admin reopens the window.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xcc" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    # No seed — the dict is empty (admin closed the window) and
    # settings.peers has no row (admin never approved).

    response = await _dispatch_intent(
        controller,
        _DispatchInput(
            intent=PeerLinkIntent.PAIR_STATUS,
            dashboard_id="alpha",
            label="",
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            peer_ip="192.168.1.10",
        ),
    )

    assert response is IntentResponse.REJECTED


# ---------------------------------------------------------------------------
# Helper-level error-path tests
#
# Cover the WS / Noise plumbing failure modes that the e2e tests
# can't reach without bringing down the test server — peer that
# never sends, peer that sends a TEXT frame, peer that sends bytes
# that don't decode as a Noise frame, peer that disconnects
# mid-write. AsyncMock-backed ws stub keeps each test focused on
# one branch.
# ---------------------------------------------------------------------------


def _make_ws_stub() -> AsyncMock:
    """Build an AsyncMock that quacks enough like ``WebSocketResponse`` for the helpers."""
    ws = AsyncMock(spec=web.WebSocketResponse)
    ws.closed = False
    return ws


def _binary_msg(data: bytes) -> WSMessage:
    return WSMessage(type=WSMsgType.BINARY, data=data, extra=None)


@pytest.mark.asyncio
async def test_read_handshake_message_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer that opens TCP but never sends msg1 falls through the timeout branch."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
        0.01,
    )
    session = PeerLinkNoiseSession.responder(secrets.token_bytes(32))
    ws = _make_ws_stub()

    async def _hang() -> WSMessage:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    ws.receive.side_effect = _hang

    result = await _read_handshake_message(session, ws, _HandshakeStep.MSG1)
    assert result is None


@pytest.mark.asyncio
async def test_read_handshake_message_non_binary_frame_returns_none() -> None:
    """A TEXT frame on the binary channel is rejected without crashing the session."""
    session = PeerLinkNoiseSession.responder(secrets.token_bytes(32))
    ws = _make_ws_stub()
    ws.receive.return_value = WSMessage(type=WSMsgType.TEXT, data="hello", extra=None)

    result = await _read_handshake_message(session, ws, _HandshakeStep.MSG1)
    assert result is None


@pytest.mark.asyncio
async def test_read_handshake_message_noise_error_returns_none() -> None:
    """Garbage bytes that don't decode as a Noise frame log a warning and return None."""
    session = PeerLinkNoiseSession.responder(secrets.token_bytes(32))
    ws = _make_ws_stub()
    # Random bytes won't decode as a valid msg1 (which expects an
    # ephemeral X25519 pubkey + AEAD tag); the noiseprotocol lib
    # raises NoiseInvalidMessage / NoiseHandshakeError.
    ws.receive.return_value = _binary_msg(b"\x00" * 16)

    result = await _read_handshake_message(session, ws, _HandshakeStep.MSG1)
    assert result is None


@pytest.mark.asyncio
async def test_send_bytes_safely_connection_reset_returns_false() -> None:
    """``ConnectionResetError`` (peer hung up) returns False without escalating the log."""
    ws = _make_ws_stub()
    ws.send_bytes.side_effect = ConnectionResetError("peer hung up")

    result = await _send_bytes_safely(ws, b"payload", log_label="msg1")
    assert result is False


@pytest.mark.asyncio
async def test_send_bytes_safely_other_exception_returns_false() -> None:
    """Other transport errors are debug-logged and the function returns False."""
    ws = _make_ws_stub()
    ws.send_bytes.side_effect = RuntimeError("ws closed mid-send")

    result = await _send_bytes_safely(ws, b"payload", log_label="msg1")
    assert result is False


@pytest.mark.asyncio
async def test_send_handshake_message_noise_error_returns_false() -> None:
    """A noise-side write failure returns False without touching the WS."""
    ws = _make_ws_stub()
    session = MagicMock(spec=PeerLinkNoiseSession)
    session.write_handshake_message.side_effect = NoiseInvalidMessage("bogus state")

    result = await _send_handshake_message(session, ws, b"", _HandshakeStep.MSG2)
    assert result is False
    ws.send_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_response_encrypt_error_skips_send() -> None:
    """``encrypt`` failing post-handshake logs a warning and skips ``send_bytes``."""
    ws = _make_ws_stub()
    session = MagicMock(spec=PeerLinkNoiseSession)
    session.encrypt.side_effect = NoiseInvalidMessage("nonce exhausted")

    await _send_response(session, ws, IntentResponse.OK)
    ws.send_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_drive_session_msg1_timeout_returns_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer that never sends msg1 closes the WS without dispatching anything."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
        0.01,
    )
    controller = MagicMock(spec=RemoteBuildController)
    ws = _make_ws_stub()

    async def _hang() -> WSMessage:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    ws.receive.side_effect = _hang

    await _drive_peer_link_session(controller, ws, "10.0.0.1", secrets.token_bytes(32))
    ws.send_bytes.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_logs_unexpected_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception inside the session driver lands in the loud-traceback branch."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_peer_link._drive_peer_link_session",
        _boom,
    )

    handler = await make_peer_link_handler(controller, tmp_path)
    request = MagicMock()
    request.remote = "10.0.0.5"
    ws_response = AsyncMock(spec=web.WebSocketResponse)
    ws_response.closed = False
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_peer_link.web.WebSocketResponse",
        lambda: ws_response,
    )

    with caplog.at_level(
        "ERROR", logger="esphome_device_builder.controllers.remote_build_peer_link"
    ):
        await handler(request)
    assert any("peer-link session error" in record.message for record in caplog.records)
    ws_response.close.assert_awaited()


def test_parse_intent_missing_key_returns_none() -> None:
    """``intent`` field missing from the dict → unknown intent."""
    assert _parse_intent(_json.dumps({"label": "alpha"})) is None


def test_parse_intent_non_string_returns_none() -> None:
    """Non-string ``intent`` value → unknown intent."""
    assert _parse_intent(_json.dumps({"intent": 42})) is None


def test_parse_intent_unknown_string_returns_none() -> None:
    """Wire string that isn't a member of ``PeerLinkIntent`` → unknown intent."""
    assert _parse_intent(_json.dumps({"intent": "evil"})) is None


def test_parse_intent_non_dict_returns_none() -> None:
    """Top-level JSON that isn't a dict → unknown intent."""
    assert _parse_intent(_json.dumps([1, 2, 3])) is None


def test_parse_json_decode_error_returns_none() -> None:
    """Garbage bytes return ``None`` instead of bubbling a decode error."""
    assert _parse_json(b"not json {") is None


def test_parse_json_empty_returns_none() -> None:
    """Empty payload short-circuits to ``None`` without invoking the decoder."""
    assert _parse_json(b"") is None


def test_normalize_label_strips_and_passes_through() -> None:
    assert _normalize_label("  Kitchen  ") == "Kitchen"


def test_normalize_label_truncates_oversized_input() -> None:
    """A peer-supplied multi-kilobyte label is silently truncated, not rejected."""
    huge = "a" * (_PEER_LABEL_MAX_CHARS * 50)
    assert len(_normalize_label(huge)) == _PEER_LABEL_MAX_CHARS


def test_normalize_label_non_string_returns_empty() -> None:
    assert _normalize_label(42) == ""
    assert _normalize_label(None) == ""


@pytest.mark.asyncio
async def test_drive_session_msg2_send_failure_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection that drops between msg1 and msg2 short-circuits the dispatch."""
    controller = MagicMock(spec=RemoteBuildController)
    initiator_priv = secrets.token_bytes(32)
    responder_priv = secrets.token_bytes(32)
    initiator = PeerLinkNoiseSession.initiator(initiator_priv)
    msg1 = initiator.write_handshake_message(_json.dumps({"intent": "preview"}))
    ws = _make_ws_stub()
    ws.receive.return_value = _binary_msg(msg1)
    # Generic transport error on send_bytes — ``_send_bytes_safely``
    # returns False, the driver bails before ever reaching dispatch.
    ws.send_bytes.side_effect = RuntimeError("ws closed mid-handshake")

    await _drive_peer_link_session(controller, ws, "10.0.0.1", responder_priv)
    # send_bytes was attempted exactly once — for msg2 — and failed;
    # the driver returned without trying to read msg3.
    assert ws.send_bytes.await_count == 1
    assert ws.receive.await_count == 1


@pytest.mark.asyncio
async def test_drive_session_unknown_intent_msg3_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown intent + peer disconnects before msg3 closes without a response frame."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
        0.01,
    )
    controller = MagicMock(spec=RemoteBuildController)
    initiator_priv = secrets.token_bytes(32)
    responder_priv = secrets.token_bytes(32)
    initiator = PeerLinkNoiseSession.initiator(initiator_priv)
    msg1 = initiator.write_handshake_message(_json.dumps({"intent": "evil"}))

    ws = _make_ws_stub()
    msg1_msg = _binary_msg(msg1)

    async def _hang_after_msg1() -> WSMessage:
        if ws.receive.await_count == 1:
            return msg1_msg
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    ws.receive.side_effect = _hang_after_msg1

    await _drive_peer_link_session(controller, ws, "10.0.0.1", responder_priv)
    # msg2 was sent; msg3 read timed out; no response frame.
    assert ws.send_bytes.await_count == 1


@pytest.mark.asyncio
async def test_drive_session_unknown_intent_msg2_send_failure() -> None:
    """Unknown intent + msg2 send fails → driver bails before reading msg3."""
    controller = MagicMock(spec=RemoteBuildController)
    initiator_priv = secrets.token_bytes(32)
    responder_priv = secrets.token_bytes(32)
    initiator = PeerLinkNoiseSession.initiator(initiator_priv)
    msg1 = initiator.write_handshake_message(_json.dumps({"intent": "evil"}))

    ws = _make_ws_stub()
    ws.receive.return_value = _binary_msg(msg1)
    ws.send_bytes.side_effect = RuntimeError("ws closed mid-handshake")

    await _drive_peer_link_session(controller, ws, "10.0.0.1", responder_priv)
    assert ws.send_bytes.await_count == 1
    assert ws.receive.await_count == 1


@pytest.mark.asyncio
async def test_drive_session_happy_path_msg3_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known intent + msg2 sends + msg3 read times out → driver bails before dispatch."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
        0.01,
    )
    controller = MagicMock(spec=RemoteBuildController)
    initiator_priv = secrets.token_bytes(32)
    responder_priv = secrets.token_bytes(32)
    initiator = PeerLinkNoiseSession.initiator(initiator_priv)
    msg1 = initiator.write_handshake_message(_json.dumps({"intent": "preview"}))

    ws = _make_ws_stub()

    async def _hang_after_msg1() -> WSMessage:
        if ws.receive.await_count == 1:
            return _binary_msg(msg1)
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    ws.receive.side_effect = _hang_after_msg1

    await _drive_peer_link_session(controller, ws, "10.0.0.1", responder_priv)
    # msg2 sent (1 send), msg3 timed out, no response sent.
    assert ws.send_bytes.await_count == 1


@pytest.mark.asyncio
async def test_drive_session_handshake_not_complete_logs_and_returns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the responder reaches msg3 without a remote static pubkey, log a warning and bail."""
    controller = MagicMock(spec=RemoteBuildController)
    responder_priv = secrets.token_bytes(32)

    # Drive through msg1+msg2+msg3 against a real session, then
    # patch ``remote_static_pub`` to raise — the only way the
    # handshake-not-complete branch fires in practice is a noise
    # library bug or a partial-frame race we can't reproduce
    # cleanly otherwise.
    initiator_priv = secrets.token_bytes(32)
    initiator = PeerLinkNoiseSession.initiator(initiator_priv)
    msg1 = initiator.write_handshake_message(_json.dumps({"intent": "preview"}))

    real_session = PeerLinkNoiseSession.responder(responder_priv)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_peer_link.PeerLinkNoiseSession.responder",
        lambda priv: real_session,
    )

    ws = _make_ws_stub()
    receive_results: list[WSMessage | None] = [_binary_msg(msg1), None]

    async def _next_receive() -> WSMessage:
        result = receive_results.pop(0)
        if result is None:
            # Simulate a finished msg2/msg3 exchange feeding the
            # initiator side and producing msg3 from it.
            msg2_bytes = ws.send_bytes.await_args.args[0]
            initiator.read_handshake_message(msg2_bytes)
            return _binary_msg(initiator.write_handshake_message(b""))
        return result

    ws.receive.side_effect = _next_receive

    # Now force the captured static to look unset post-handshake.
    with monkeypatch.context() as m:
        m.setattr(
            type(real_session),
            "remote_static_pub",
            property(
                fget=lambda self: (_ for _ in ()).throw(HandshakeNotCompleteError("simulated"))
            ),
        )
        with caplog.at_level(
            "WARNING",
            logger="esphome_device_builder.controllers.remote_build_peer_link",
        ):
            await _drive_peer_link_session(controller, ws, "10.0.0.1", responder_priv)

    assert any("did not yield remote static pubkey" in record.message for record in caplog.records)


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

    # PENDING entries land in the in-memory dict, not on disk.
    loop = asyncio.get_running_loop()
    settings = await loop.run_in_executor(
        None, load_remote_build_settings, controller._db.settings.config_dir
    )
    assert settings.peers == []
    pending = controller._pending_peers["alpha"]
    assert pending.label == "alpha"
    # The receiver's controller derived the pin from the
    # handshake transcript's authenticated initiator static
    # pubkey, not from anything in msg3.
    assert pending.static_x25519_pub == round_trip.initiator_static_pub
    assert pending.pin_sha256 == pin_sha256_for_pubkey(round_trip.initiator_static_pub)


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
async def test_e2e_non_dict_msg3_payload_treated_as_empty(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """A msg3 JSON list isn't a crash; treated as empty dict, REJECTED via dashboard_id gate."""
    client, _, _ = peer_link_app

    initiator_priv = secrets.token_bytes(32)
    session = PeerLinkNoiseSession.initiator(initiator_priv)
    ws = await client.ws_connect(PEER_LINK_PATH)
    try:
        msg1 = session.write_handshake_message(_json.dumps({"intent": "peer_link"}))
        await ws.send_bytes(msg1)
        msg2 = await ws.receive_bytes()
        session.read_handshake_message(msg2)
        # JSON array — valid JSON but not a dict; ``.get()`` would
        # crash without the isinstance check.
        msg3 = session.write_handshake_message(_json.dumps([1, 2, 3]))
        await ws.send_bytes(msg3)
        encrypted = await ws.receive_bytes()
    finally:
        await ws.close()

    # Empty dashboard_id (because msg3 didn't carry one) → REJECTED.
    assert _decode_intent_response(session, encrypted) == IntentResponse.REJECTED


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
