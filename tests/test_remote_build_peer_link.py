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
import json
import secrets
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import WSMessage, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from esphome.const import __version__ as esphome_version
from noise.exceptions import NoiseInvalidMessage
from noise.exceptions import NoiseInvalidMessage as _NoiseInvalidMessage

from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build import (
    peer_link as _peer_link_module,
)
from esphome_device_builder.controllers.remote_build.job_fanout import JobFanout
from esphome_device_builder.controllers.remote_build.peer_link import (
    _PEER_LABEL_MAX_CHARS,
    APP_FRAME_MAX_BYTES,
    PEER_LINK_PATH,
    PeerLinkChannel,
    PeerLinkSession,
    TerminateReason,
    _dispatch_intent,
    _DispatchInput,
    _drive_peer_link_session,
    _HandshakeStep,
    _normalize_label,
    _parse_intent,
    _parse_json,
    _read_handshake_message,
    _receive_loop,
    _send_bytes_safely,
    _send_handshake_message,
    _send_response,
    make_peer_link_handler,
)
from esphome_device_builder.controllers.remote_build.submit_job import (
    SubmitJobReceiver,
)
from esphome_device_builder.helpers import json as _json
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
    IntentResponse,
    PeerLinkIntent,
    StoredPeer,
)

from .conftest import (
    make_remote_build_controller,
    make_submit_job_frames,
    make_tar_bundle,
)


def _make_controller(*, config_dir: Any = None) -> RemoteBuildController:
    return make_remote_build_controller(config_dir=config_dir)


def _seed_peer(controller: RemoteBuildController, peer: StoredPeer) -> None:
    """Insert *peer* into the controller's RAM-canonical APPROVED dict."""
    controller._approved_peers[peer.dashboard_id] = peer


async def _wait_until(condition: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Yield to the loop until *condition()* returns truthy or *timeout* elapses.

    Raises :exc:`TimeoutError` (via :func:`asyncio.wait_for`) when
    the condition stays false past *timeout* — surfaces a
    deterministic failure in place of a silent
    ``for _ in range(N): sleep(0)`` loop that would otherwise
    fall through and let the next assertion produce a misleading
    error message. Use for waits whose synchronisation source is
    a piece of mutated state (registry dict membership, attribute
    flip) rather than a callback we can wire an
    :class:`asyncio.Event` into.
    """

    async def _spin() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(_spin(), timeout=timeout)


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

    assert controller._approved_peers == {}
    assert controller._pending_peers == {}


@pytest.mark.asyncio
async def test_dispatch_peer_link_approved_returns_ok(tmp_path: Path) -> None:
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    pubkey = b"\xbb" * 32
    pin = hashlib.sha256(pubkey).hexdigest()
    _seed_peer(
        controller,
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
    assert controller._approved_peers == {}
    assert controller._pending_peers == {}
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
    assert controller._approved_peers == {}
    assert controller._pending_peers == {}
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
        "esphome_device_builder.controllers.remote_build.peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
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
async def test_send_response_advertises_esphome_version() -> None:
    """The ``intent_response`` body carries the receiver's esphome version.

    Pins the wire contract that unblocks pick_build_path's
    version-compat gate: the receiver's response carries
    ``intent_response`` (the discriminator the offloader already
    branches on) AND ``esphome_version`` (the version the
    offloader stores on :class:`StoredPairing` and the
    version-compat gate eventually compares against the
    offloader's own bundled :mod:`esphome` version). The body
    is JSON-encoded before encrypt; this test reads back the
    plaintext bytes :func:`session.encrypt` was called with so
    a regression that drops the field or changes its name trips
    here instead of producing a silent empty value on the
    offloader.
    """
    ws = _make_ws_stub()
    session = MagicMock(spec=PeerLinkNoiseSession)
    session.encrypt.return_value = b"ciphertext-stub"

    await _send_response(session, ws, IntentResponse.OK)

    session.encrypt.assert_called_once()
    body = session.encrypt.call_args.args[0]
    parsed = json.loads(body)
    assert parsed == {
        "intent_response": IntentResponse.OK.value,
        "esphome_version": esphome_version,
    }


@pytest.mark.asyncio
async def test_drive_session_msg1_timeout_returns_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer that never sends msg1 closes the WS without dispatching anything."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
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
        "esphome_device_builder.controllers.remote_build.peer_link._drive_peer_link_session",
        _boom,
    )

    handler = await make_peer_link_handler(controller, tmp_path)
    request = MagicMock()
    request.remote = "10.0.0.5"
    ws_response = AsyncMock(spec=web.WebSocketResponse)
    ws_response.closed = False
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.peer_link.web.WebSocketResponse",
        lambda: ws_response,
    )

    with caplog.at_level(
        "ERROR", logger="esphome_device_builder.controllers.remote_build.peer_link"
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
        "esphome_device_builder.controllers.remote_build.peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
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
        "esphome_device_builder.controllers.remote_build.peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS",
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
        "esphome_device_builder.controllers.remote_build.peer_link.PeerLinkNoiseSession.responder",
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
            logger="esphome_device_builder.controllers.remote_build.peer_link",
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

    # PENDING entries land in the in-memory dict; APPROVED dict
    # stays empty until admin clicks Accept.
    assert controller._approved_peers == {}
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

    # Closed window short-circuits before any RAM mutation.
    assert controller._approved_peers == {}
    assert controller._pending_peers == {}


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
    _seed_peer(
        controller,
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


# ---------------------------------------------------------------------------
# Phase 5a-1 — long-lived peer-link session: keep WS open after intent_response,
# encrypted ping/pong heartbeat, structured terminate close, controller-side
# session registry.
# ---------------------------------------------------------------------------


async def _drive_peer_link_session_open(
    client: TestClient,
    *,
    dashboard_id: str,
    initiator_priv: bytes | None = None,
) -> tuple[PeerLinkNoiseSession, Any, bytes]:
    """
    Run the handshake + intent_response and return the still-open WS.

    Caller is responsible for closing the WS. Use this for tests
    that need to drive application frames after the OK response.
    Returns ``(session, ws, initiator_pub)``.
    """
    if initiator_priv is None:
        initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    session = PeerLinkNoiseSession.initiator(initiator_priv)
    ws = await client.ws_connect(PEER_LINK_PATH)
    msg1 = session.write_handshake_message(_json.dumps({"intent": "peer_link"}))
    await ws.send_bytes(msg1)
    msg2 = await ws.receive_bytes()
    session.read_handshake_message(msg2)
    msg3 = session.write_handshake_message(_json.dumps({"dashboard_id": dashboard_id}))
    await ws.send_bytes(msg3)
    encrypted_response = await ws.receive_bytes()
    assert _decode_intent_response(session, encrypted_response) == IntentResponse.OK
    # The receiver pushes a one-shot ``queue_status`` frame right
    # after registering the session (cold-connect signal for the
    # 7a-3 install scheduler). Drain it here so callers asserting
    # on subsequent application frames don't have to special-case
    # the initial push at every call site.
    initial_app_frame = await ws.receive_bytes()
    assert _decode_app_frame(session, initial_app_frame)["type"] == "queue_status"
    return session, ws, initiator_pub


async def _seed_approved_offloader(
    controller: RemoteBuildController,
    *,
    dashboard_id: str,
    pubkey: bytes,
) -> None:
    """Seed an APPROVED ``StoredPeer`` whose pubkey matches *pubkey*."""
    pin = hashlib.sha256(pubkey).hexdigest()
    _seed_peer(
        controller,
        StoredPeer(
            dashboard_id=dashboard_id,
            pin_sha256=pin,
            static_x25519_pub=pubkey,
            label=dashboard_id,
            paired_at=1.0,
        ),
    )


def _decode_app_frame(session: PeerLinkNoiseSession, encrypted: bytes) -> dict[str, Any]:
    """Decrypt + JSON-parse a post-handshake application frame."""
    parsed = _json.loads(session.decrypt(encrypted))
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.asyncio
async def test_e2e_peer_link_session_stays_open_after_intent_response(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """The receiver doesn't close the WS after ``intent_response: ok`` for ``intent="peer_link"``.

    Pins the foundational 5a-1 contract: a successful peer_link
    auth keeps the session open for application messages
    (5b-5d). Pre-5a-1 the receiver closed immediately; this test
    catches a regression to that shape.
    """
    client, controller, _ = peer_link_app
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    await _seed_approved_offloader(controller, dashboard_id="alpha", pubkey=initiator_pub)

    _session, ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    try:
        # Wait for the handler to drop into the receive loop —
        # registration happens just before. If the WS closed
        # instead, ``"alpha"`` would never land in the dict and
        # the wait would time out (deterministic failure).
        await _wait_until(lambda: "alpha" in controller._peer_link_sessions)
        assert not ws.closed
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_e2e_peer_link_session_responds_to_offloader_ping(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """An offloader-side ``ping`` gets a ``pong`` echoing the same nonce.

    The receiver is also sending its own pings (5a-1's heartbeat
    loop), but the offloader-driven ping path is what the 5a-2
    client side will rely on if it picks up bidirectional
    keepalive — pin the parity now so 5a-2 doesn't have to
    rediscover it.
    """
    client, controller, _ = peer_link_app
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    await _seed_approved_offloader(controller, dashboard_id="alpha", pubkey=initiator_pub)

    session, ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    try:
        ping = session.encrypt(_json.dumps({"type": "ping", "nonce": 42}))
        await ws.send_bytes(ping)
        pong_encrypted = await asyncio.wait_for(ws.receive_bytes(), timeout=2.0)
    finally:
        await ws.close()

    pong = _decode_app_frame(session, pong_encrypted)
    assert pong["type"] == "pong"
    assert pong["nonce"] == 42


@pytest.mark.asyncio
async def test_e2e_peer_link_session_kicks_old_on_duplicate_connect(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """A second connect from the same dashboard_id terminates the older session.

    Both sessions share the same ``dashboard_id``; the receiver
    can have at most one active session per peer (issue's
    "Connection lifecycle" spec). The duplicate-connect path
    sends ``terminate{reason: superseded}`` to the older
    session and closes its WS, freeing the registry slot for
    the new connect. A restarted offloader gets its slot back
    rather than doubling.
    """
    client, controller, _ = peer_link_app
    # Both sessions present the SAME initiator pubkey because
    # the same dashboard_id has to map to the same stored peer
    # (the receiver pins on pubkey-hash, not dashboard_id alone).
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    await _seed_approved_offloader(controller, dashboard_id="alpha", pubkey=initiator_pub)

    old_session, old_ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    _new_session, new_ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    try:
        # The old session should receive a ``terminate`` frame
        # carrying ``reason: superseded`` before the WS closes.
        terminate_encrypted = await asyncio.wait_for(old_ws.receive_bytes(), timeout=2.0)
        terminate = _decode_app_frame(old_session, terminate_encrypted)
        assert terminate["type"] == "terminate"
        assert terminate["reason"] == TerminateReason.SUPERSEDED.value
        # Receive one more frame to drive the WS through the
        # CLOSE transition; aiohttp's client side only flips
        # ``closed`` on the next ``receive()``-style call.
        close_msg = await asyncio.wait_for(old_ws.receive(), timeout=2.0)
        assert close_msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
        # The registry now holds the NEW session; the old one is gone.
        assert "alpha" in controller._peer_link_sessions
    finally:
        await new_ws.close()


@pytest.mark.asyncio
async def test_e2e_peer_link_session_unregistered_on_peer_close(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """Closing the WS from the offloader side drops the registry entry."""
    client, controller, _ = peer_link_app
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    await _seed_approved_offloader(controller, dashboard_id="alpha", pubkey=initiator_pub)

    _session, ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    await _wait_until(lambda: "alpha" in controller._peer_link_sessions)

    await ws.close()

    # The receiver's session loop sees the close, exits, and
    # ``unregister_peer_link_session`` runs in its ``finally``.
    await _wait_until(lambda: "alpha" not in controller._peer_link_sessions)


@pytest.mark.asyncio
async def test_e2e_peer_link_session_drained_on_controller_stop(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """``RemoteBuildController.stop()`` terminates active peer-link sessions.

    The shutdown path snapshots the session dict, sends a
    structured ``terminate{reason: server_shutting_down}`` to
    each, and closes the WS. The session loop's ``finally``
    runs ``unregister``; ``stop()`` then ``clear()``s the dict
    belt-and-braces.
    """
    client, controller, _ = peer_link_app
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    await _seed_approved_offloader(controller, dashboard_id="alpha", pubkey=initiator_pub)

    session, ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    try:
        await _wait_until(lambda: "alpha" in controller._peer_link_sessions)

        # ``stop()`` runs in the same loop; the test fixture
        # also calls ``stop()`` on teardown but we drive it
        # explicitly here so we can assert on the terminate
        # frame the offloader sees.
        stop_task = asyncio.create_task(controller.stop())

        terminate_encrypted = await asyncio.wait_for(ws.receive_bytes(), timeout=2.0)
        terminate = _decode_app_frame(session, terminate_encrypted)
        assert terminate["type"] == "terminate"
        assert terminate["reason"] == TerminateReason.SERVER_SHUTTING_DOWN.value

        await stop_task
        assert controller._peer_link_sessions == {}
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_e2e_peer_link_session_oversize_frame_terminates(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
) -> None:
    """A frame past ``APP_FRAME_MAX_BYTES`` triggers ``terminate{malformed_frame}``.

    Closes a misbehaving / hostile peer at the dispatch seam
    rather than letting an unbounded ciphertext pin memory.
    Sized via ``ws_connect(max_msg_size=...)`` because aiohttp's
    default 4 MiB cap would catch this before the application
    layer does — we want to verify *our* check fires.
    """
    client, controller, _ = peer_link_app
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    await _seed_approved_offloader(controller, dashboard_id="alpha", pubkey=initiator_pub)

    session, ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    try:
        # Encrypt a payload larger than the cap. ChaCha20 adds a
        # 16-byte auth tag; the encrypted size is plaintext + 16.
        oversize = session.encrypt(b"x" * (APP_FRAME_MAX_BYTES + 1))
        await ws.send_bytes(oversize)
        terminate_encrypted = await asyncio.wait_for(ws.receive_bytes(), timeout=2.0)
        terminate = _decode_app_frame(session, terminate_encrypted)
        assert terminate["type"] == "terminate"
        assert terminate["reason"] == TerminateReason.MALFORMED_FRAME.value
    finally:
        await ws.close()


def _install_stub_submit_job_receiver(
    controller: RemoteBuildController,
) -> tuple[Any, list[Any]]:
    """Wire a stub firmware controller into a fresh :class:`SubmitJobReceiver`.

    The ``peer_link_app`` fixture doesn't drive
    ``RemoteBuildController.start()``, so
    ``_submit_job_receiver`` would be ``None`` at dispatch
    time. Tests that exercise the wire-side ``submit_job`` flow
    install a fresh receiver here. Returns the firmware stub +
    a list that captures every queued :class:`FirmwareJob`.
    """
    queued_jobs: list[Any] = []
    firmware_stub = MagicMock()

    def _create_job(
        *,
        configuration: str,
        job_type: Any,
        remote_peer: str = "",
        remote_peer_label: str = "",
        remote_job_id: str = "",
        device_name: str = "",
        device_friendly_name: str = "",
    ) -> Any:
        job = MagicMock()
        job.job_id = f"local-{len(queued_jobs)}"
        job.configuration = configuration
        job.remote_peer = remote_peer
        job.remote_peer_label = remote_peer_label
        job.remote_job_id = remote_job_id
        job.device_name = device_name
        job.device_friendly_name = device_friendly_name
        return job

    async def _enqueue(job: Any) -> Any:
        queued_jobs.append(job)
        return job

    firmware_stub._create_job = MagicMock(side_effect=_create_job)
    firmware_stub._enqueue = AsyncMock(side_effect=_enqueue)
    controller._submit_job_receiver = SubmitJobReceiver(
        config_dir=controller._db.settings.config_dir,
        firmware_controller=firmware_stub,
    )
    return firmware_stub, queued_jobs


@pytest.mark.asyncio
async def test_e2e_submit_job_dispatches_to_receiver(
    peer_link_app: tuple[TestClient, RemoteBuildController, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``submit_job`` header + chunks over the wire dispatches to the receiver.

    Drives the receive-loop branches in
    :func:`controllers.remote_build.peer_link._receive_loop`
    that route ``SUBMIT_JOB`` / ``SUBMIT_JOB_CHUNK`` to the
    :class:`SubmitJobReceiver`. Stubs
    :func:`prepare_bundle_for_compile` because the real
    extractor expects an esphome-shaped manifest; the contract
    being pinned is the wire dispatch + bundle write + queue
    plumbing, not the extraction itself (which has its own
    tests in :mod:`esphome`).
    """
    client, controller, _ = peer_link_app
    initiator_priv = X25519PrivateKey.generate().private_bytes_raw()
    initiator_pub = (
        X25519PrivateKey.from_private_bytes(initiator_priv).public_key().public_bytes_raw()
    )
    await _seed_approved_offloader(controller, dashboard_id="alpha", pubkey=initiator_pub)
    _firmware_stub, queued_jobs = _install_stub_submit_job_receiver(controller)
    extracted_path = (
        controller._db.settings.config_dir
        / ".esphome"
        / ".remote_builds"
        / "alpha"
        / "kitchen"
        / "kitchen.yaml"
    )

    def _stub_prepare(bundle_path: Path, target_dir: Path) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        extracted_path.parent.mkdir(parents=True, exist_ok=True)
        extracted_path.write_bytes(b"esphome:\n  name: kitchen\n")
        return extracted_path

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.submit_job.prepare_bundle_for_compile",
        _stub_prepare,
    )
    bundle = make_tar_bundle("kitchen.yaml", b"esphome:\n  name: kitchen\n")
    header, chunks = make_submit_job_frames(
        job_id="wire-job",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle=bundle,
    )

    session, ws, _ = await _drive_peer_link_session_open(
        client, dashboard_id="alpha", initiator_priv=initiator_priv
    )
    try:
        await ws.send_bytes(session.encrypt(_json.dumps(header)))
        for chunk in chunks:
            await ws.send_bytes(session.encrypt(_json.dumps(chunk)))
        ack_encrypted = await asyncio.wait_for(ws.receive_bytes(), timeout=2.0)
    finally:
        await ws.close()

    ack = _decode_app_frame(session, ack_encrypted)
    assert ack["type"] == "submit_job_ack"
    assert ack["job_id"] == "wire-job"
    assert ack["accepted"] is True
    assert "reason" not in ack

    assert len(queued_jobs) == 1
    assert queued_jobs[0].remote_peer == "alpha"


# ---------------------------------------------------------------------------
# Pure-unit tests: PeerLinkSession.send_app_frame, registry methods
# ---------------------------------------------------------------------------


def _noise_pair() -> tuple[PeerLinkNoiseSession, PeerLinkNoiseSession]:
    """Drive a 3-message Noise XX handshake against itself.

    Returns ``(initiator, responder)`` with the handshake
    finalised on both sides — application encrypts on either
    side decrypt cleanly on the other. Drops the static pubkey
    parity assertion to stay focused; tests that care about
    the captured pubkey assert it themselves.
    """
    initiator = PeerLinkNoiseSession.initiator(secrets.token_bytes(32))
    responder = PeerLinkNoiseSession.responder(secrets.token_bytes(32))
    msg1 = initiator.write_handshake_message(b"")
    responder.read_handshake_message(msg1)
    msg2 = responder.write_handshake_message(b"")
    initiator.read_handshake_message(msg2)
    msg3 = initiator.write_handshake_message(b"")
    responder.read_handshake_message(msg3)
    return initiator, responder


class _FakeWs:
    """In-memory ``WebSocketResponse`` stand-in for unit tests.

    Captures every ``send_bytes`` payload + counts ``close``
    calls so tests can assert on the number / shape of sends
    without standing up an aiohttp test server. Async-iterable
    so :func:`_receive_loop` can iterate it directly: pre-load
    ``inbox`` with the script of inbound :class:`WSMessage`
    frames; the iterator yields each in order and then exits
    (mirrors aiohttp's natural CLOSE-on-iterator-exhaustion
    behaviour).
    """

    def __init__(self, inbox: list[WSMessage] | None = None) -> None:
        self.sends: list[bytes] = []
        self.closes: int = 0
        self.closed: bool = False
        self._inbox: list[WSMessage] = list(inbox) if inbox else []

    async def send_bytes(self, data: bytes) -> None:
        self.sends.append(data)

    async def close(self) -> None:
        self.closes += 1
        self.closed = True

    def __aiter__(self) -> _FakeWs:
        return self

    async def __anext__(self) -> WSMessage:
        if not self._inbox:
            raise StopAsyncIteration
        return self._inbox.pop(0)


def _make_unit_session(noise: PeerLinkNoiseSession) -> tuple[PeerLinkSession, _FakeWs]:
    """Build a :class:`PeerLinkSession` wired against a fresh :class:`_FakeWs`."""
    ws = _FakeWs()
    return PeerLinkSession(
        dashboard_id="alpha",
        ws=ws,  # type: ignore[arg-type]
        noise=noise,
        peer_ip="127.0.0.1",
    ), ws


@pytest.mark.asyncio
async def test_peer_link_session_send_app_frame_is_serialised(tmp_path: Path) -> None:
    """The session's send lock keeps concurrent encrypts from interleaving.

    Noise's cipher state advances its nonce per encrypt and is
    not safe to share across concurrent calls. The send lock
    serialises encrypt + ws-write so the wire order matches the
    encrypt order (which is what the peer's decrypt-by-position
    requires).
    """
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)

    # Fire two concurrent send_app_frame calls; the lock should
    # serialise them so the responder's encrypt nonce advances
    # in order, and decoding from the initiator side returns the
    # frames in send-order.
    await asyncio.gather(
        session.send_app_frame({"type": "ping", "n": 1}),
        session.send_app_frame({"type": "ping", "n": 2}),
    )
    assert len(ws.sends) == 2
    decoded = [_json.loads(initiator.decrypt(frame)) for frame in ws.sends]
    assert {entry["n"] for entry in decoded} == {1, 2}


@pytest.mark.asyncio
async def test_peer_link_session_terminate_idempotent(tmp_path: Path) -> None:
    """Calling ``terminate`` twice doesn't double-send the frame or double-close the WS."""
    _initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)

    await session.terminate(TerminateReason.SUPERSEDED)
    await session.terminate(TerminateReason.SUPERSEDED)
    assert len(ws.sends) == 1
    assert ws.closes == 1


@pytest.mark.asyncio
async def test_send_app_frame_short_circuits_after_terminate(tmp_path: Path) -> None:
    """A late ``send_app_frame`` after ``terminate`` returns False without a wire frame.

    Pins the no-race contract: the heartbeat task or a future
    application sender that wakes from ``asyncio.sleep`` after
    the controller flipped ``_closing`` mustn't push a final
    ``ping`` onto the wire after the ``terminate`` frame has
    already gone out. The frame count after ``terminate`` is
    exactly 1 (the terminate frame itself).
    """
    _initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)

    await session.terminate(TerminateReason.SUPERSEDED)
    assert len(ws.sends) == 1  # the terminate frame

    sent = await session.send_app_frame({"type": "ping", "nonce": 99})
    assert sent is False
    # Still just the one frame from terminate; the late ping
    # didn't sneak through.
    assert len(ws.sends) == 1


@pytest.mark.asyncio
async def test_register_peer_link_session_kicks_existing(tmp_path: Path) -> None:
    """Registering a new session for the same dashboard_id terminates the existing one."""
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    # Stub two sessions with mocked terminate.
    old = MagicMock(spec=PeerLinkSession)
    old.dashboard_id = "alpha"
    old.terminate = AsyncMock()
    new = MagicMock(spec=PeerLinkSession)
    new.dashboard_id = "alpha"
    new.terminate = AsyncMock()

    await controller.register_peer_link_session(old)
    assert controller._peer_link_sessions["alpha"] is old
    old.terminate.assert_not_called()

    await controller.register_peer_link_session(new)
    assert controller._peer_link_sessions["alpha"] is new
    old.terminate.assert_awaited_once_with(TerminateReason.SUPERSEDED)
    new.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_register_peer_link_session_pushes_initial_queue_status(tmp_path: Path) -> None:
    """A freshly-registered session gets a one-shot ``queue_status`` frame.

    The cold-connect signal for the 7a-3 install scheduler: the
    transition-driven broadcast (``_on_firmware_queue_transition``)
    only fires when the receiver's local firmware queue mutates.
    Without the initial push, an offloader that pairs against a
    receiver whose queue is permanently idle would never see a
    ``_peer_queue_status`` entry, and ``pick_build_path`` would
    silently fall back to LOCAL on every install request.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    controller._db.firmware = MagicMock()
    controller._db.firmware.queue_status_snapshot = MagicMock(return_value=(True, False, 0))

    session = MagicMock(spec=PeerLinkSession)
    session.dashboard_id = "alpha"
    session.send_app_frame = AsyncMock(return_value=True)

    await controller.register_peer_link_session(session)
    # The send is dispatched as a background task; let it drain.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    session.send_app_frame.assert_awaited_once_with(
        {"type": "queue_status", "idle": True, "running": False, "queue_depth": 0}
    )


@pytest.mark.asyncio
async def test_register_peer_link_session_skips_initial_queue_status_when_firmware_missing(
    tmp_path: Path,
) -> None:
    """A controller without a firmware backend registers without a push.

    Firmware queue is wired lazily by ``DeviceBuilder.start()``;
    a session-register that races a not-yet-wired
    :attr:`_db.firmware` must not crash. Mirror of the
    ``self._db.firmware is None`` short-circuit in
    :meth:`_on_firmware_queue_transition`.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    controller._db.firmware = None  # production pre-``start`` state

    session = MagicMock(spec=PeerLinkSession)
    session.dashboard_id = "alpha"
    session.send_app_frame = AsyncMock()

    await controller.register_peer_link_session(session)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    session.send_app_frame.assert_not_called()


@pytest.mark.asyncio
async def test_register_peer_link_session_swallows_snapshot_exception(tmp_path: Path) -> None:
    """A ``queue_status_snapshot`` raise mustn't poison session registration.

    Best-effort contract: the initial push is a cold-connect
    optimisation, not a load-bearing step. A raise from
    ``firmware.queue_status_snapshot`` (mock contract drift,
    unexpected internal error, etc.) gets logged and swallowed
    so the session still registers cleanly; the transition-
    driven broadcast catches the offloader up on the next
    queue change. Mirrors the swallow-and-log stance of
    :meth:`_broadcast_queue_status` for per-session sends.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    controller._db.firmware = MagicMock()
    controller._db.firmware.queue_status_snapshot = MagicMock(side_effect=RuntimeError("boom"))

    session = MagicMock(spec=PeerLinkSession)
    session.dashboard_id = "alpha"
    session.send_app_frame = AsyncMock()

    # Must not raise — register completes cleanly even on a bad
    # snapshot read.
    await controller.register_peer_link_session(session)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Push skipped, but the session is registered.
    session.send_app_frame.assert_not_called()
    assert controller._peer_link_sessions["alpha"] is session


@pytest.mark.asyncio
async def test_register_peer_link_session_swallows_send_app_frame_exception(
    tmp_path: Path,
) -> None:
    """A ``send_app_frame`` raise inside the background task is logged, not propagated.

    ``send_app_frame`` already swallows the common transport /
    encrypt / serialise failures and returns ``False``; the
    inner ``except Exception`` in :meth:`_send_initial_queue_status`
    is the catch-all for an unexpected raise (mock contract
    drift, future code path that raises before the inner gate).
    Pins that an unhandled raise from the send doesn't leak into
    the background task's exception handler — the offloader
    catches up on the next queue transition instead.
    """
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()
    controller._db.firmware = MagicMock()
    controller._db.firmware.queue_status_snapshot = MagicMock(return_value=(True, False, 0))

    session = MagicMock(spec=PeerLinkSession)
    session.dashboard_id = "alpha"
    session.send_app_frame = AsyncMock(side_effect=RuntimeError("boom"))

    await controller.register_peer_link_session(session)
    # Drain the background task; the inner swallow must keep
    # the raise from surfacing as an unhandled task exception.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    session.send_app_frame.assert_awaited_once()
    # Session still registered — the failed push doesn't roll it back.
    assert controller._peer_link_sessions["alpha"] is session


def test_unregister_peer_link_session_no_op_when_replaced(tmp_path: Path) -> None:
    """Unregistering a session that has already been replaced doesn't evict the new one."""
    controller = _make_controller(config_dir=tmp_path)

    old = MagicMock(spec=PeerLinkSession)
    old.dashboard_id = "alpha"
    new = MagicMock(spec=PeerLinkSession)
    new.dashboard_id = "alpha"

    controller._peer_link_sessions["alpha"] = new
    controller.unregister_peer_link_session(old)
    # New session still in place — the old session's late
    # cleanup didn't evict the replacement.
    assert controller._peer_link_sessions["alpha"] is new


def test_unregister_peer_link_session_removes_when_current(tmp_path: Path) -> None:
    """Unregistering the currently-registered session drops the entry."""
    controller = _make_controller(config_dir=tmp_path)

    session = MagicMock(spec=PeerLinkSession)
    session.dashboard_id = "alpha"
    controller._peer_link_sessions["alpha"] = session

    controller.unregister_peer_link_session(session)
    assert controller._peer_link_sessions == {}


@pytest.mark.asyncio
async def test_send_app_frame_returns_false_on_unserialisable_payload(tmp_path: Path) -> None:
    """A payload with a non-JSON-encodable value short-circuits before encrypting."""
    _initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)

    # ``object()`` is not JSON-serialisable — orjson raises TypeError.
    sent = await session.send_app_frame({"type": "ping", "junk": object()})
    assert sent is False
    assert ws.sends == []


@pytest.mark.asyncio
async def test_send_app_frame_returns_false_on_noise_encrypt_failure(tmp_path: Path) -> None:
    """A Noise-side failure surfaces as ``False``, not an unhandled exception.

    Forces the failure by bumping the Noise nonce past its 2^64
    cap is impractical; just monkey-patch ``noise.encrypt`` to
    raise. The branch we're covering is the ``except NOISE_ERRORS``
    block — any of the ``NOISE_ERRORS`` tuple's exception types
    is sufficient.
    """
    _initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)

    real_encrypt = responder.encrypt

    def _encrypt_fails(plaintext: bytes) -> bytes:
        raise _NoiseInvalidMessage("test stub")

    responder.encrypt = _encrypt_fails  # type: ignore[method-assign]
    try:
        sent = await session.send_app_frame({"type": "ping"})
    finally:
        responder.encrypt = real_encrypt  # type: ignore[method-assign]
    assert sent is False
    assert ws.sends == []


@pytest.mark.asyncio
async def test_peer_link_channel_send_terminate_swallows_aiohttp_close_error(
    tmp_path: Path,
) -> None:
    """``send_terminate`` returns cleanly when ``ws.close()`` raises ``aiohttp.ClientError``.

    :class:`PeerLinkChannel` runs on both sides of the wire — the
    offloader-side ``self.ws`` is an
    :class:`aiohttp.ClientWebSocketResponse` whose ``.close()``
    can raise ``ClientConnectionError`` / ``ClientError`` when
    the peer has already gone away. Without widening the
    suppression around the close, that exception would escape
    and could block a caller's :class:`CancelledError`
    propagation (e.g. inside
    :meth:`PeerLinkClient._run_one_session`'s cancellation
    handler that awaits ``send_terminate`` before re-raising).
    """
    _initiator, responder = _noise_pair()

    class _RaisingCloseWs:
        def __init__(self) -> None:
            self.sends: list[bytes] = []
            self.closed = False

        async def send_bytes(self, data: bytes) -> None:
            self.sends.append(data)

        async def close(self) -> None:
            self.closed = True
            raise aiohttp.ClientConnectionError("forced for test")

    ws = _RaisingCloseWs()
    channel = PeerLinkChannel(noise=responder, ws=ws, log_label="test")  # type: ignore[arg-type]

    # Should NOT raise — the suppression around ``ws.close()``
    # widened to include ``aiohttp.ClientError``.
    await channel.send_terminate(TerminateReason.SERVER_SHUTTING_DOWN.value)

    # The terminate frame was still sent before the close attempt.
    assert len(ws.sends) == 1
    assert ws.closed is True


# ---------------------------------------------------------------------------
# Receive loop unit tests — drive ``_receive_loop`` against a scripted ``_FakeWs``
# so each malformed-frame branch fires its terminate cleanly.
# ---------------------------------------------------------------------------


def _binary_msg(data: bytes) -> WSMessage:
    """Construct an aiohttp ``WSMessage`` carrying *data* as a BINARY frame."""
    return WSMessage(type=WSMsgType.BINARY, data=data, extra="")


def _text_msg(data: str) -> WSMessage:
    """Construct a TEXT-typed ``WSMessage``."""
    return WSMessage(type=WSMsgType.TEXT, data=data, extra="")


@pytest.mark.asyncio
async def test_receive_loop_terminates_on_text_frame(tmp_path: Path) -> None:
    """A TEXT message (not BINARY) triggers ``terminate{malformed_frame}``."""
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    ws._inbox.append(_text_msg("hello"))

    await _receive_loop(session, MagicMock())

    # One terminate frame sent + WS closed; decode confirms the
    # reason field carries the expected ``malformed_frame``.
    assert len(ws.sends) == 1
    assert ws.closes == 1
    decoded = _json.loads(initiator.decrypt(ws.sends[0]))
    assert decoded["type"] == "terminate"
    assert decoded["reason"] == TerminateReason.MALFORMED_FRAME.value


@pytest.mark.asyncio
async def test_receive_loop_terminates_on_undecryptable_frame(tmp_path: Path) -> None:
    """Random bytes that fail Noise decrypt trigger ``terminate{malformed_frame}``."""
    _initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    # 64 bytes of random garbage — not a valid ChaCha20-Poly1305
    # frame against ``responder``'s current cipher state.
    ws._inbox.append(_binary_msg(b"\xde\xad\xbe\xef" * 16))

    await _receive_loop(session, MagicMock())

    assert ws.closes == 1
    # The terminate frame itself was sent before close — exact
    # decryption check is in the e2e tests; here we just pin the
    # branch ran.
    assert len(ws.sends) == 1


@pytest.mark.asyncio
async def test_receive_loop_terminates_on_non_object_json(tmp_path: Path) -> None:
    """Encrypted JSON that isn't an object (e.g. a list) triggers terminate."""
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    # Valid Noise frame carrying ``[1,2,3]`` plaintext.
    bad_payload = initiator.encrypt(_json.dumps([1, 2, 3]))
    ws._inbox.append(_binary_msg(bad_payload))

    await _receive_loop(session, MagicMock())

    assert ws.closes == 1
    assert len(ws.sends) == 1


@pytest.mark.asyncio
async def test_receive_loop_routes_cancel_job_to_controller(tmp_path: Path) -> None:
    """A ``cancel_job`` Noise frame routes through ``controller.handle_cancel_job``."""
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    frame = initiator.encrypt(_json.dumps({"type": "cancel_job", "job_id": "j-1"}))
    ws._inbox.append(_binary_msg(frame))

    controller = MagicMock()
    controller.handle_cancel_job = AsyncMock()
    await _receive_loop(session, controller)

    controller.handle_cancel_job.assert_awaited_once()
    call_session, call_frame = controller.handle_cancel_job.await_args.args
    assert call_session is session
    assert call_frame == {"type": "cancel_job", "job_id": "j-1"}


@pytest.mark.asyncio
async def test_receive_loop_routes_download_artifacts_to_sender(tmp_path: Path) -> None:
    """A ``download_artifacts`` Noise frame routes through the artifacts-download sender."""
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    payload = {"type": "download_artifacts", "job_id": "remote-7"}
    frame = initiator.encrypt(_json.dumps(payload))
    ws._inbox.append(_binary_msg(frame))

    sender = MagicMock()
    sender.handle_download_artifacts = AsyncMock()
    controller = MagicMock()
    controller.get_artifacts_download_sender = MagicMock(return_value=sender)
    await _receive_loop(session, controller)

    controller.get_artifacts_download_sender.assert_called_once()
    sender.handle_download_artifacts.assert_awaited_once()
    call_session, call_frame = sender.handle_download_artifacts.await_args.args
    assert call_session is session
    assert call_frame == payload


@pytest.mark.asyncio
async def test_receive_loop_pong_updates_last_pong_at(tmp_path: Path) -> None:
    """A ``pong`` frame from the peer bumps ``session.last_pong_at``."""
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    session.last_pong_at = 0.0
    pong = initiator.encrypt(_json.dumps({"type": "pong", "nonce": 7}))
    ws._inbox.append(_binary_msg(pong))

    await _receive_loop(session, MagicMock())

    assert session.last_pong_at > 0.0
    # No outbound frame fired (pong is one-way from peer to us).
    assert ws.sends == []


@pytest.mark.asyncio
async def test_receive_loop_peer_terminate_exits_cleanly(tmp_path: Path) -> None:
    """A peer-side ``terminate`` exits the loop without sending our own."""
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    bye = initiator.encrypt(_json.dumps({"type": "terminate", "reason": "client_quit"}))
    ws._inbox.append(_binary_msg(bye))

    await _receive_loop(session, MagicMock())

    # No echo terminate, no close from our side — caller (the
    # session-loop driver) closes the WS in its outer finally.
    assert ws.sends == []
    assert ws.closes == 0
    assert session._closing is True


@pytest.mark.asyncio
async def test_receive_loop_unknown_app_frame_type_logged_and_ignored(tmp_path: Path) -> None:
    """An unknown ``type`` field in a well-formed encrypted frame is logged at debug, not fatal.

    Pins forward-compat: a future application message type from a
    newer offloader must not crash an older receiver — we just
    skip it. 5b-5d adding new types lands fine against this seam.
    """
    initiator, responder = _noise_pair()
    session, ws = _make_unit_session(responder)
    unknown = initiator.encrypt(_json.dumps({"type": "from_the_future"}))
    ws._inbox.append(_binary_msg(unknown))
    # Loop should consume the unknown frame and then exit when the
    # iterator is empty (no further frames). Add an explicit close
    # message? Async-iter exit on StopAsyncIteration is enough.

    await _receive_loop(session, MagicMock())

    # Nothing sent, no terminate, no close from our side.
    assert ws.sends == []
    assert ws.closes == 0


# ---------------------------------------------------------------------------
# Heartbeat loop unit tests — fast clock + monkey-patched sleep so the test
# doesn't actually wait 30s.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_peer_link_heartbeat_terminates_on_pong_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shared heartbeat helper invokes ``on_dead`` when no pong lands in time.

    Shrinks the heartbeat interval so the test finishes quickly
    (real ``asyncio.sleep``, no stubbing). Drives the clock past
    the miss threshold on the next ``_monotonic`` read so the
    very first iteration trips the timeout branch.
    """
    monkeypatch.setattr(_peer_link_module, "HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(_peer_link_module, "HEARTBEAT_DEAD_AFTER_SECONDS", 0.0)
    monkeypatch.setattr(_peer_link_module, "_monotonic", lambda: 1000.0)

    pings: list[int] = []
    deaths: list[bool] = []

    async def _send_ping(nonce: int) -> bool:
        pings.append(nonce)
        return True

    async def _on_dead() -> None:
        deaths.append(True)

    await _peer_link_module.run_peer_link_heartbeat(
        send_ping=_send_ping,
        last_pong_at=lambda: 0.0,
        on_dead=_on_dead,
    )

    # Timeout branch fires before the first ping — last_pong_at
    # is at 0, _monotonic() is at 1000, the gap exceeds the
    # zero threshold.
    assert pings == []
    assert deaths == [True]


@pytest.mark.asyncio
async def test_run_peer_link_heartbeat_terminates_on_send_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If ``send_ping`` reports failure (WS dead), the heartbeat invokes ``on_dead``."""
    monkeypatch.setattr(_peer_link_module, "HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(_peer_link_module, "_monotonic", lambda: 0.0)

    deaths: list[bool] = []

    async def _send_ping_fail(_nonce: int) -> bool:
        return False

    async def _on_dead() -> None:
        deaths.append(True)

    await _peer_link_module.run_peer_link_heartbeat(
        send_ping=_send_ping_fail,
        last_pong_at=lambda: 0.0,
        on_dead=_on_dead,
    )

    assert deaths == [True]


@pytest.mark.asyncio
async def test_run_peer_link_session_heartbeat_closures_route_to_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closures wired into ``run_peer_link_heartbeat`` route to the live session.

    Covers the receiver-side ``_send_ping`` and ``_on_dead``
    closure bodies inside :func:`_run_peer_link_session` (the
    short two-line wrappers that translate the heartbeat helper's
    callback contract onto the session's
    :meth:`PeerLinkSession.send_app_frame` and
    :meth:`PeerLinkSession.terminate`). The other heartbeat tests
    drive :func:`run_peer_link_heartbeat` directly with stubbed
    callbacks and so don't touch these wrappers.

    Captures both callbacks via a stub heartbeat, fires them, and
    asserts the wire effects: a ``ping`` frame on the WS for
    ``send_ping``, and a ``terminate{heartbeat_timeout}`` frame
    plus a CLOSE for ``on_dead``.
    """
    initiator, responder = _noise_pair()
    parked = asyncio.Event()

    class _ParkingWs(_FakeWs):
        async def __anext__(self) -> WSMessage:
            # Block ``_receive_loop`` so the heartbeat task gets a
            # turn to capture the callbacks before the session's
            # lifecycle ends. Cancellation of the run task wakes
            # the wait via :class:`CancelledError`.
            await parked.wait()
            raise StopAsyncIteration

    ws = _ParkingWs()
    controller = _make_controller(config_dir=tmp_path)
    controller._db.bus = MagicMock()

    captured: dict[str, Any] = {}
    heartbeat_started = asyncio.Event()

    async def _capturing_heartbeat(*, send_ping: Any, last_pong_at: Any, on_dead: Any) -> None:
        captured["send_ping"] = send_ping
        captured["on_dead"] = on_dead
        heartbeat_started.set()
        # Park until the test signals via cancellation.
        await asyncio.Event().wait()

    monkeypatch.setattr(_peer_link_module, "run_peer_link_heartbeat", _capturing_heartbeat)

    run_task = asyncio.create_task(
        _peer_link_module._run_peer_link_session(
            controller=controller,
            ws=ws,  # type: ignore[arg-type]
            session=responder,
            dashboard_id="alpha",
            peer_ip="127.0.0.1",
        )
    )
    # Wait for ``_run_peer_link_session`` to register and kick off
    # the heartbeat task — the helper sets ``heartbeat_started``
    # the moment its callbacks land in ``captured``.
    await asyncio.wait_for(heartbeat_started.wait(), timeout=2.0)
    # ``register_peer_link_session`` now schedules a one-shot
    # ``queue_status`` push on session open (cold-connect signal
    # for the 7a-3 install scheduler). Wait for that frame to
    # land in ``ws.sends`` and decrypt it so the initiator's
    # nonce counter advances; subsequent ping / terminate
    # decrypts on the same Noise session are then aligned.
    while not ws.sends:
        await asyncio.sleep(0)
    initial = _json.loads(initiator.decrypt(ws.sends[-1]))
    assert initial["type"] == "queue_status"

    # Exercise ``_send_ping`` — the closure encrypts and sends a
    # ping frame through ``send_app_frame``.
    ok = await captured["send_ping"](7)
    assert ok is True
    decrypted = _json.loads(initiator.decrypt(ws.sends[-1]))
    assert decrypted == {"type": "ping", "nonce": 7}

    # Exercise ``_on_dead`` — the closure calls
    # ``session.terminate(HEARTBEAT_TIMEOUT)`` which encrypts a
    # ``terminate`` frame, sends it, then closes the WS.
    await captured["on_dead"]()
    final = _json.loads(initiator.decrypt(ws.sends[-1]))
    assert final == {"type": "terminate", "reason": "heartbeat_timeout"}
    assert ws.closed

    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_peer_link_heartbeat_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling the heartbeat task surfaces as ``CancelledError``, not a silent return.

    Pin the no-swallow contract: catching ``CancelledError``
    inside the loop would hide the cancellation signal from the
    parent coroutine. The parent's ``contextlib.suppress(CancelledError)``
    is the right layer to absorb it.
    """
    # Use a long interval so the task is reliably parked in
    # ``asyncio.sleep`` when we cancel.
    monkeypatch.setattr(_peer_link_module, "HEARTBEAT_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(_peer_link_module, "_monotonic", lambda: 0.0)

    async def _noop_send(_nonce: int) -> bool:
        return True

    async def _noop_dead() -> None:
        pass

    task = asyncio.create_task(
        _peer_link_module.run_peer_link_heartbeat(
            send_ping=_noop_send,
            last_pong_at=lambda: 0.0,
            on_dead=_noop_dead,
        )
    )
    # Yield once so the task enters its sleep; then cancel.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Phase 5d: cancel_job receiver-side handler
# ---------------------------------------------------------------------------


def _make_receiver_with_fanout(tmp_path: Path) -> RemoteBuildController:
    """Build a receiver controller with a wired-but-not-started JobFanout.

    Lets tests pre-populate ``_job_fanout._remote_jobs`` to
    simulate the state the controller would be in after a
    real ``submit_job`` had queued a remote job.
    """
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller._db.firmware = MagicMock()
    controller._db.firmware.cancel = AsyncMock()
    controller._job_fanout = JobFanout(controller)
    return controller


def _cancel_session(dashboard_id: str = "alpha") -> Any:
    """Minimal :class:`PeerLinkSession` stand-in with the dashboard_id attribute."""
    session = MagicMock()
    session.dashboard_id = dashboard_id
    return session


@pytest.mark.asyncio
async def test_handle_cancel_job_routes_to_firmware_cancel(tmp_path: Path) -> None:
    """Happy path: resolve offloader job_id → firmware job_id → fire ``firmware.cancel``."""
    controller = _make_receiver_with_fanout(tmp_path)
    assert controller._job_fanout is not None
    controller._job_fanout._remote_jobs["fw-abc"] = ("offloader-1", "remote-xyz")

    await controller.handle_cancel_job(
        _cancel_session(dashboard_id="offloader-1"),
        {"type": "cancel_job", "job_id": "remote-xyz"},
    )
    controller._db.firmware.cancel.assert_awaited_once_with(job_id="fw-abc")


@pytest.mark.asyncio
async def test_handle_cancel_job_unknown_remote_job_drops_silently(tmp_path: Path) -> None:
    """No matching correlation entry: drop without raising or calling firmware.cancel."""
    controller = _make_receiver_with_fanout(tmp_path)
    await controller.handle_cancel_job(
        _cancel_session(dashboard_id="offloader-1"),
        {"type": "cancel_job", "job_id": "unknown"},
    )
    controller._db.firmware.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_handle_cancel_job_pin_to_wrong_session_no_cancel(tmp_path: Path) -> None:
    """A cancel_job arriving on a different session than the submit-time peer is dropped.

    Protection against a paired offloader cancelling a different
    offloader's job — the firmware controller's existing
    ``_remote_jobs`` cache keys on ``(remote_peer,
    remote_job_id)`` so even a same-``remote_job_id`` collision
    from a different peer doesn't resolve.
    """
    controller = _make_receiver_with_fanout(tmp_path)
    assert controller._job_fanout is not None
    controller._job_fanout._remote_jobs["fw-abc"] = ("offloader-1", "j-1")
    await controller.handle_cancel_job(
        _cancel_session(dashboard_id="offloader-2"),  # different peer
        {"type": "cancel_job", "job_id": "j-1"},
    )
    controller._db.firmware.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_handle_cancel_job_malformed_frame_drops_silently(tmp_path: Path) -> None:
    """A cancel_job frame missing ``job_id`` is dropped without raising."""
    controller = _make_receiver_with_fanout(tmp_path)
    await controller.handle_cancel_job(
        _cancel_session(),
        {"type": "cancel_job"},  # missing job_id
    )
    controller._db.firmware.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_handle_cancel_job_before_controller_started_drops_silently(
    tmp_path: Path,
) -> None:
    """A cancel_job arriving before :meth:`start` ran is dropped silently.

    Defensive branch — covers the cold-start race where the
    peer-link listener could (in theory) accept a session
    before the controller's ``start`` has wired up
    ``_job_fanout`` and the firmware controller reference.
    In production the listener doesn't bind until ``start``
    completes, but the guard surfaces the dependency
    explicitly.
    """
    controller = _make_receiver_with_fanout(tmp_path)
    controller._job_fanout = None  # simulate pre-start
    await controller.handle_cancel_job(
        _cancel_session(dashboard_id="offloader-1"),
        {"type": "cancel_job", "job_id": "j-1"},
    )
    controller._db.firmware.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_handle_cancel_job_swallows_firmware_command_error(tmp_path: Path) -> None:
    """A ``CommandError`` from ``firmware.cancel`` (e.g. already-terminal) is swallowed."""
    controller = _make_receiver_with_fanout(tmp_path)
    assert controller._job_fanout is not None
    controller._job_fanout._remote_jobs["fw-abc"] = ("offloader-1", "j-1")
    controller._db.firmware.cancel = AsyncMock(
        side_effect=CommandError(ErrorCode.INVALID_ARGS, "Cannot cancel a completed job")
    )
    # Should not raise — the cancel is best-effort.
    await controller.handle_cancel_job(
        _cancel_session(dashboard_id="offloader-1"),
        {"type": "cancel_job", "job_id": "j-1"},
    )
    controller._db.firmware.cancel.assert_awaited_once()
