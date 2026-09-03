"""Tests for the receiver → offloader connect-back flow."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from esphome_device_builder._remote_build_lifecycle import RemoteBuildLifecycle
from esphome_device_builder.api.ws import init_ws_app
from esphome_device_builder.controllers.remote_build import (
    ReceiverController,
)
from esphome_device_builder.controllers.remote_build import (
    connect_back as rb_connect_back,
)
from esphome_device_builder.controllers.remote_build import pair_status as rb_pair_status
from esphome_device_builder.controllers.remote_build import (
    peer_link_lifecycle as rb_peer_link_lifecycle,
)
from esphome_device_builder.controllers.remote_build import rebind as rb_rebind
from esphome_device_builder.controllers.remote_build._client_models import (
    InitiatorRoundTrip,
    PairStatusResult,
)
from esphome_device_builder.controllers.remote_build._intent import IntentOutcome
from esphome_device_builder.controllers.remote_build._models import (
    RebindProbeOutcome,
    RebindProbeResult,
)
from esphome_device_builder.controllers.remote_build.peer_link import (
    PEER_LINK_PATH,
    _dispatch_intent,
    _DispatchInput,
    make_peer_link_handler,
)
from esphome_device_builder.controllers.remote_build.peer_link.handshake import (
    _msg3_port,
)
from esphome_device_builder.controllers.remote_build.peer_link_client import (
    PeerLinkClient,
    PeerLinkClientError,
    PeerLinkPinMismatchError,
)
from esphome_device_builder.helpers import json as _json
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.peer_link_identity import PeerLinkIdentityStore
from esphome_device_builder.helpers.peer_link_noise import (
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
    public_bytes_for_priv,
)
from esphome_device_builder.models import (
    EventType,
    IntentResponse,
    PeerLinkIntent,
    PeerStatus,
    RejectReason,
    StoredPairing,
    StoredPeer,
)

from .conftest import RemoteBuildTestHandles as RemoteBuildController
from .conftest import make_remote_build_controller

PIN = "a" * 64
IP = "192.0.2.10"
_CONNECT_BACK_LOGGER = rb_connect_back.__name__
_HANDSHAKE_LOGGER = "esphome_device_builder.controllers.remote_build.peer_link.handshake"


def _stored_pairing(*, status: PeerStatus = PeerStatus.APPROVED) -> StoredPairing:
    return StoredPairing(
        receiver_hostname="build.local",
        receiver_port=6055,
        pin_sha256=PIN,
        static_x25519_pub=b"\x01" * 32,
        label="desktop",
        paired_at=1.0,
        status=status,
    )


def _offloader_with_pairing(
    config_dir: Path, *, status: PeerStatus = PeerStatus.APPROVED
) -> tuple[RemoteBuildController, StoredPairing]:
    controller = make_remote_build_controller(config_dir=config_dir)
    controller.offloader.state.offloader_peer_link_priv = secrets.token_bytes(32)
    pairing = _stored_pairing(status=status)
    controller.offloader.state.pairings[pairing.pin_sha256] = pairing
    return controller, pairing


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch,
    controller: RemoteBuildController,
    outcome: RebindProbeOutcome,
) -> tuple[AsyncMock, AsyncMock]:
    probe = AsyncMock(return_value=RebindProbeResult(outcome))
    commit = AsyncMock()
    monkeypatch.setattr(controller.offloader, "_probe_pairing_endpoint", probe)
    monkeypatch.setattr(controller.offloader, "_commit_endpoint_rebind", commit)
    return probe, commit


def _stored_peer(
    *,
    dashboard_id: str = "alpha",
    peer_ip: str = "127.0.0.1",
    connect_back_port: int = 6055,
    pin_sha256: str = PIN,
) -> StoredPeer:
    return StoredPeer(
        dashboard_id=dashboard_id,
        pin_sha256=pin_sha256,
        static_x25519_pub=b"\x11" * 32,
        label="alpha",
        paired_at=1.0,
        peer_ip=peer_ip,
        connect_back_port=connect_back_port,
    )


def _round_trip(intent_response: str, reason: str | None = None) -> InitiatorRoundTrip:
    response: dict[str, Any] = {"intent_response": intent_response}
    if reason is not None:
        response["reason"] = reason
    return InitiatorRoundTrip(
        intent_response=intent_response, remote_static_pub=b"\x22" * 32, response=response
    )


# ---------------------------------------------------------------------------
# Offloader-side handle_connect_back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [pytest.param(None, id="unknown_pin"), pytest.param(PeerStatus.PENDING, id="pending")]
)
async def test_connect_back_unapproved_pin_rejected(
    tmp_path: Path, status: PeerStatus | None
) -> None:
    if status is None:
        controller = make_remote_build_controller(config_dir=tmp_path)
    else:
        controller, _ = _offloader_with_pairing(tmp_path, status=status)
    outcome = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=IP, announced_port=6055
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.NO_APPROVED_PEER)


@pytest.mark.parametrize(
    ("peer_ip", "port"),
    [
        pytest.param(IP, 0, id="port_zero"),
        pytest.param(IP, 70000, id="port_oversize"),
        pytest.param("not-an-ip", 6055, id="bad_ip"),
        pytest.param("", 6055, id="empty_ip"),
    ],
)
async def test_connect_back_bad_endpoint_rejected(tmp_path: Path, peer_ip: str, port: int) -> None:
    controller, _ = _offloader_with_pairing(tmp_path)
    outcome = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=peer_ip, announced_port=port
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.BAD_ENDPOINT)


async def test_connect_back_without_identity_rejected(tmp_path: Path) -> None:
    controller, _ = _offloader_with_pairing(tmp_path)
    controller.offloader.state.offloader_peer_link_priv = None
    outcome = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=IP, announced_port=6055
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.PROBE_FAILED)


async def test_connect_back_live_forward_link_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _ = _offloader_with_pairing(tmp_path)
    controller.offloader.state.open_peer_links.add(PIN)
    probe, commit = _patch_probe(monkeypatch, controller, RebindProbeOutcome.OK)
    outcome = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=IP, announced_port=6055
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.ALREADY_CONNECTED)
    probe.assert_not_awaited()
    commit.assert_not_awaited()


async def test_connect_back_inflight_probe_holds_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _ = _offloader_with_pairing(tmp_path)
    controller.offloader.state.rebind_probe_until[PIN] = time.monotonic() + 100
    probe, commit = _patch_probe(monkeypatch, controller, RebindProbeOutcome.OK)
    outcome = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=IP, announced_port=6055
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.REBIND_IN_PROGRESS)
    probe.assert_not_awaited()
    commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        pytest.param(RebindProbeOutcome.UNREACHABLE, RejectReason.PROBE_FAILED, id="unreachable"),
        pytest.param(RebindProbeOutcome.PIN_MISMATCH, RejectReason.PROBE_FAILED, id="mismatch"),
        pytest.param(
            RebindProbeOutcome.PAIRING_REPLACED, RejectReason.NO_APPROVED_PEER, id="replaced"
        ),
        pytest.param(
            RebindProbeOutcome.STATUS_CHANGED, RejectReason.NO_APPROVED_PEER, id="status_changed"
        ),
    ],
)
async def test_connect_back_probe_failure_persists_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: RebindProbeOutcome,
    reason: RejectReason,
) -> None:
    controller, pairing = _offloader_with_pairing(tmp_path)
    _, commit = _patch_probe(monkeypatch, controller, outcome)
    result = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=IP, announced_port=6123
    )
    assert result == IntentOutcome(IntentResponse.REJECTED, reason)
    commit.assert_not_awaited()
    assert (pairing.receiver_hostname, pairing.receiver_port) == ("build.local", 6055)
    # Cooldown stays seeded so a dial burst can't re-probe immediately.
    assert controller.offloader.state.rebind_probe_until[PIN] > time.monotonic()


@pytest.mark.parametrize("endpoint_changed", [True, False], ids=["moved", "unchanged"])
async def test_connect_back_probe_ok_commits_source_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint_changed: bool
) -> None:
    controller, pairing = _offloader_with_pairing(tmp_path)
    if not endpoint_changed:
        pairing.receiver_hostname = IP
        pairing.receiver_port = 6123
    probe, commit = _patch_probe(monkeypatch, controller, RebindProbeOutcome.OK)
    outcome = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=IP, announced_port=6123
    )
    assert outcome == IntentOutcome(IntentResponse.OK)
    probe.assert_awaited_once_with(pairing=pairing, new_hostname=IP, new_port=6123)
    commit.assert_awaited_once_with(pairing, hostname=IP, port=6123)


# ---------------------------------------------------------------------------
# Intent dispatch routing
# ---------------------------------------------------------------------------


def _dispatch_input(intent: PeerLinkIntent, **overrides: Any) -> _DispatchInput:
    fields: dict[str, Any] = {
        "intent": intent,
        "dashboard_id": "alpha",
        "label": "alpha",
        "pin_sha256": PIN,
        "static_x25519_pub": b"\x01" * 32,
        "peer_ip": IP,
    }
    fields.update(overrides)
    return _DispatchInput(**fields)


async def test_dispatch_connect_back_without_offloader_rejected() -> None:
    controller = MagicMock(spec=ReceiverController)
    outcome = await _dispatch_intent(
        controller,
        _dispatch_input(
            PeerLinkIntent.CONNECT_BACK, dashboard_id="", announced_peer_link_port=6055
        ),
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.PROBE_FAILED)


async def test_dispatch_connect_back_routes_to_offloader_without_dashboard_id_gate() -> None:
    controller = MagicMock(spec=ReceiverController)
    offloader = MagicMock()
    offloader._handle_connect_back = AsyncMock(return_value=IntentOutcome(IntentResponse.OK))
    outcome = await _dispatch_intent(
        controller,
        _dispatch_input(
            PeerLinkIntent.CONNECT_BACK, dashboard_id="", announced_peer_link_port=6123
        ),
        offloader=offloader,
    )
    assert outcome == IntentOutcome(IntentResponse.OK)
    offloader._handle_connect_back.assert_awaited_once_with(
        pin_sha256=PIN, peer_ip=IP, announced_port=6123
    )


@pytest.mark.parametrize(
    "intent",
    [
        PeerLinkIntent.PREVIEW,
        PeerLinkIntent.PAIR_REQUEST,
        PeerLinkIntent.PEER_LINK,
        PeerLinkIntent.PAIR_STATUS,
    ],
)
async def test_dispatch_receiver_intents_refused_when_gated(intent: PeerLinkIntent) -> None:
    controller = MagicMock(spec=ReceiverController)
    outcome = await _dispatch_intent(
        controller, _dispatch_input(intent), accept_receiver_intents=False
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.BAD_INTENT)
    controller.record_pair_request.assert_not_called()
    controller.lookup_peer_for_session.assert_not_called()
    controller.lookup_peer_for_status.assert_not_called()


# ---------------------------------------------------------------------------
# Wire round-trip against an offloader-only listener
# ---------------------------------------------------------------------------


async def _drive_round_trip(
    client: TestClient,
    msg1_payload: dict[str, Any],
    msg3_payload: dict[str, Any],
    initiator_priv: bytes,
) -> tuple[dict[str, Any], bool]:
    """Drive one Noise XX round-trip; return (decoded response, ws closed by peer)."""
    session = PeerLinkNoiseSession.initiator(initiator_priv)
    ws = await client.ws_connect(PEER_LINK_PATH)
    try:
        await ws.send_bytes(session.write_handshake_message(_json.dumps(msg1_payload)))
        session.read_handshake_message(await ws.receive_bytes())
        await ws.send_bytes(session.write_handshake_message(_json.dumps(msg3_payload)))
        response = _json.loads(session.decrypt(await ws.receive_bytes()))
        closed_by_peer = (await ws.receive()).type.name in ("CLOSE", "CLOSING", "CLOSED")
    finally:
        await ws.close()
    return response, closed_by_peer


async def test_wire_connect_back_round_trip_offloader_only_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect_back reaches the offloader handler with the source IP; ws closes after reply."""
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    identity = await PeerLinkIdentityStore(tmp_path).async_load()
    initiator_priv = secrets.token_bytes(32)
    initiator_pin = pin_sha256_for_pubkey(public_bytes_for_priv(initiator_priv))

    seen: dict[str, Any] = {}

    async def _handle(**kwargs: Any) -> IntentOutcome:
        seen.update(kwargs)
        return IntentOutcome(IntentResponse.OK)

    monkeypatch.setattr(controller.offloader, "_handle_connect_back", _handle)

    app = web.Application()
    init_ws_app(app)
    handler = make_peer_link_handler(
        controller.receiver, identity, offloader=controller.offloader, accept_receiver_intents=False
    )
    app.router.add_get(PEER_LINK_PATH, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response, closed_by_peer = await _drive_round_trip(
            client, {"intent": "connect_back"}, {"peer_link_port": 6123}, initiator_priv
        )
    finally:
        await client.close()

    assert response["intent_response"] == IntentResponse.OK.value
    assert closed_by_peer
    assert seen == {"pin_sha256": initiator_pin, "peer_ip": "127.0.0.1", "announced_port": 6123}


async def test_wire_receiver_intents_rejected_on_offloader_only_listener(tmp_path: Path) -> None:
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    identity = await PeerLinkIdentityStore(tmp_path).async_load()

    app = web.Application()
    init_ws_app(app)
    handler = make_peer_link_handler(
        controller.receiver, identity, offloader=controller.offloader, accept_receiver_intents=False
    )
    app.router.add_get(PEER_LINK_PATH, handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response, _ = await _drive_round_trip(
            client,
            {"intent": "pair_request"},
            {"dashboard_id": "alpha", "label": "alpha"},
            secrets.token_bytes(32),
        )
    finally:
        await client.close()

    assert response["intent_response"] == IntentResponse.REJECTED.value
    assert response["reason"] == RejectReason.BAD_INTENT.value
    assert controller.receiver.state.pending_peers == {}


# ---------------------------------------------------------------------------
# Forward client announces its connect-back port in msg3
# ---------------------------------------------------------------------------


async def _capture_client_msg3(get_connect_back_port: Any) -> dict[str, Any]:
    """Run a PeerLinkClient against a rejecting responder; return its msg3 payload."""
    receiver_priv = secrets.token_bytes(32)
    receiver_pub = public_bytes_for_priv(receiver_priv)
    msg3_payloads: list[bytes] = []

    async def _responder(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sess = PeerLinkNoiseSession.responder(receiver_priv)
        sess.read_handshake_message(await ws.receive_bytes())
        await ws.send_bytes(sess.write_handshake_message(b""))
        msg3_payloads.append(sess.read_handshake_message(await ws.receive_bytes()))
        # Terminal reject orphans the client so ``run()`` returns.
        await ws.send_bytes(
            sess.encrypt(b'{"intent_response": "rejected", "reason": "no_approved_peer"}')
        )
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get(PEER_LINK_PATH, _responder)
    server = TestServer(app)
    await server.start_server()
    try:
        client = PeerLinkClient(
            receiver_hostname="127.0.0.1",
            receiver_port=server.port or 0,
            identity_priv=secrets.token_bytes(32),
            dashboard_id="alpha",
            pinned_static_x25519_pub=receiver_pub,
            pin_sha256=pin_sha256_for_pubkey(receiver_pub),
            receiver_label="test-receiver",
            bus=MagicMock(),
            get_connect_back_port=get_connect_back_port,
        )
        await asyncio.wait_for(client.run(), timeout=10.0)
    finally:
        await server.close()
    assert len(msg3_payloads) == 1
    return _json.loads(msg3_payloads[0])


async def test_client_msg3_carries_connect_back_port_when_bound() -> None:
    msg3 = await _capture_client_msg3(lambda: 6123)
    assert msg3["connect_back_port"] == 6123


async def test_client_msg3_omits_connect_back_port_when_unbound() -> None:
    msg3 = await _capture_client_msg3(lambda: None)
    assert "connect_back_port" not in msg3


# ---------------------------------------------------------------------------
# Receiver-side session persistence
# ---------------------------------------------------------------------------


def _session_mock(*, peer_ip: str, connect_back_port: int) -> MagicMock:
    session = MagicMock()
    session.dashboard_id = "alpha"
    session.peer_friendly_name = ""
    session.peer_ha_addon = False
    session.peer_ip = peer_ip
    session.peer_connect_back_port = connect_back_port
    session.send_app_frame = AsyncMock(return_value=True)
    return session


async def test_register_session_refreshes_peer_ip_and_connect_back_port(tmp_path: Path) -> None:
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    save = MagicMock()
    controller.receiver._peers_store.async_delay_save = save  # type: ignore[method-assign]
    peer = _stored_peer(peer_ip="192.168.1.10", connect_back_port=0)
    controller.receiver.state.approved_peers["alpha"] = peer

    await controller.receiver.register_peer_link_session(
        _session_mock(peer_ip="10.0.0.9", connect_back_port=6123)
    )
    assert (peer.peer_ip, peer.connect_back_port) == ("10.0.0.9", 6123)
    save.assert_called()


async def test_register_session_absent_values_never_clobber(tmp_path: Path) -> None:
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.bus = MagicMock()
    peer = _stored_peer(peer_ip="10.0.0.9", connect_back_port=6123)
    controller.receiver.state.approved_peers["alpha"] = peer

    await controller.receiver.register_peer_link_session(
        _session_mock(peer_ip="", connect_back_port=0)
    )
    assert (peer.peer_ip, peer.connect_back_port) == ("10.0.0.9", 6123)


# ---------------------------------------------------------------------------
# Receiver dial sweep + backoff
# ---------------------------------------------------------------------------


def _receiver_ready_to_dial(config_dir: Path) -> tuple[RemoteBuildController, StoredPeer]:
    controller = make_remote_build_controller(config_dir=config_dir)
    db = controller.receiver._db
    db.remote_build_listener_port = 6055
    db.remote_build_receiver_role_active = True
    peer = _stored_peer()
    controller.receiver.state.approved_peers[peer.dashboard_id] = peer
    controller.receiver.state.connect_back_last_contact[peer.dashboard_id] = (
        time.monotonic() - 10_000
    )
    return controller, peer


async def test_sweep_dials_quiet_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    dialed: list[tuple[str, int]] = []

    async def _fake_dial(
        _controller: ReceiverController, peer: StoredPeer, *, announce_port: int
    ) -> None:
        dialed.append((peer.dashboard_id, announce_port))

    monkeypatch.setattr(rb_connect_back, "_dial_peer", _fake_dial)
    rb_connect_back.sweep_connect_back(controller.receiver)
    await controller.receiver.state.connect_back_tasks["alpha"]
    await asyncio.sleep(0)
    assert dialed == [("alpha", 6055)]
    assert "alpha" not in controller.receiver.state.connect_back_tasks


def _block_live_session(receiver: ReceiverController, peer: StoredPeer) -> None:
    receiver.state.peer_link_sessions["alpha"] = MagicMock()


def _block_no_port(receiver: ReceiverController, peer: StoredPeer) -> None:
    peer.connect_back_port = 0


def _block_no_ip(receiver: ReceiverController, peer: StoredPeer) -> None:
    peer.peer_ip = ""


def _block_not_quiet(receiver: ReceiverController, peer: StoredPeer) -> None:
    receiver.state.connect_back_last_contact["alpha"] = time.monotonic()


def _block_cooldown(receiver: ReceiverController, peer: StoredPeer) -> None:
    receiver.state.connect_back_cooldowns.set("alpha", 100.0)


def _block_inflight(receiver: ReceiverController, peer: StoredPeer) -> None:
    receiver.state.connect_back_tasks["alpha"] = MagicMock()


def _block_role_off(receiver: ReceiverController, peer: StoredPeer) -> None:
    receiver._db.remote_build_receiver_role_active = False


def _block_listener_down(receiver: ReceiverController, peer: StoredPeer) -> None:
    receiver._db.remote_build_listener_port = None


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(_block_live_session, id="live_session"),
        pytest.param(_block_no_port, id="no_port"),
        pytest.param(_block_no_ip, id="no_ip"),
        pytest.param(_block_not_quiet, id="not_quiet"),
        pytest.param(_block_cooldown, id="cooldown"),
        pytest.param(_block_inflight, id="inflight"),
        pytest.param(_block_role_off, id="role_off"),
        pytest.param(_block_listener_down, id="listener_down"),
    ],
)
async def test_sweep_eligibility_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    block: Callable[[ReceiverController, StoredPeer], None],
) -> None:
    controller, peer = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    block(receiver, peer)

    dial = AsyncMock()
    monkeypatch.setattr(rb_connect_back, "_dial_peer", dial)
    rb_connect_back.sweep_connect_back(receiver)
    for task in list(receiver.state.connect_back_tasks.values()):
        if isinstance(task, asyncio.Task):
            await task
    dial.assert_not_awaited()


async def test_fresh_peer_waits_one_quiet_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer never seen before is stamped now, not dialed immediately."""
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    receiver.state.connect_back_last_contact.clear()
    dial = AsyncMock()
    monkeypatch.setattr(rb_connect_back, "_dial_peer", dial)
    rb_connect_back.sweep_connect_back(receiver)
    dial.assert_not_awaited()
    assert "alpha" in receiver.state.connect_back_last_contact


async def test_session_register_cancels_dial_and_resets_backoff(tmp_path: Path) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    task = asyncio.create_task(asyncio.sleep(30))
    receiver.state.connect_back_tasks["alpha"] = task
    receiver.state.connect_back_cooldowns.escalate("alpha", 100.0, 1000.0)

    rb_connect_back.on_session_registered(receiver, "alpha")

    with pytest.raises(asyncio.CancelledError):
        await task
    assert receiver.state.connect_back_cooldowns.ready("alpha")
    assert receiver.state.connect_back_cooldowns.strikes("alpha") == 0
    assert receiver.state.connect_back_last_contact["alpha"] == pytest.approx(
        time.monotonic(), abs=5.0
    )


@pytest.mark.parametrize(
    ("reason", "min_remaining", "max_remaining"),
    [
        pytest.param(
            RejectReason.REBIND_IN_PROGRESS.value,
            rb_connect_back._CONNECT_BACK_SHORT_RETRY_SECONDS * 0.7,
            rb_connect_back._CONNECT_BACK_SHORT_RETRY_SECONDS * 1.3,
            id="rebind_in_progress",
        ),
        pytest.param(
            RejectReason.ALREADY_CONNECTED.value,
            rb_connect_back._CONNECT_BACK_RETRY_BASE_SECONDS * 0.7,
            rb_connect_back._CONNECT_BACK_RETRY_BASE_SECONDS * 1.3,
            id="already_connected",
        ),
        pytest.param(
            RejectReason.PROBE_FAILED.value,
            rb_connect_back._CONNECT_BACK_RETRY_BASE_SECONDS * 0.7,
            rb_connect_back._CONNECT_BACK_RETRY_BASE_SECONDS * 1.3,
            id="probe_failed",
        ),
        pytest.param(
            RejectReason.BAD_INTENT.value,
            rb_connect_back._CONNECT_BACK_RETRY_CAP_SECONDS - 5,
            rb_connect_back._CONNECT_BACK_RETRY_CAP_SECONDS,
            id="bad_intent_parks_at_cap",
        ),
        pytest.param(
            "some-future-reason",
            rb_connect_back._CONNECT_BACK_RETRY_CAP_SECONDS - 5,
            rb_connect_back._CONNECT_BACK_RETRY_CAP_SECONDS,
            id="unknown_parks_at_cap",
        ),
    ],
)
async def test_reply_backoff_mapping(
    tmp_path: Path, reason: str, min_remaining: float, max_remaining: float
) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    rb_connect_back._apply_reply(receiver, "alpha", _round_trip("rejected", reason))
    remaining = receiver.state.connect_back_cooldowns.remaining("alpha")
    assert min_remaining <= remaining <= max_remaining


async def test_reply_ok_resets_backoff_and_quiet_clock(tmp_path: Path) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    receiver.state.connect_back_cooldowns.escalate("alpha", 100.0, 1000.0)
    rb_connect_back._apply_reply(receiver, "alpha", _round_trip("ok"))
    assert receiver.state.connect_back_cooldowns.ready("alpha")
    assert receiver.state.connect_back_cooldowns.strikes("alpha") == 0
    assert receiver.state.connect_back_last_contact["alpha"] == pytest.approx(
        time.monotonic(), abs=5.0
    )


@pytest.mark.parametrize(
    ("side_effect", "level", "snippet"),
    [
        pytest.param(PeerLinkClientError("refused"), "DEBUG", "failed", id="transport"),
        pytest.param(
            PeerLinkPinMismatchError(b"\x33" * 32), "WARNING", "static key mismatch", id="pin"
        ),
        pytest.param(RuntimeError("boom"), "ERROR", "failed unexpectedly", id="unexpected"),
    ],
)
async def test_dial_failure_escalates_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    side_effect: BaseException,
    level: str,
    snippet: str,
) -> None:
    controller, peer = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    monkeypatch.setattr(
        rb_connect_back, "drive_initiator_round_trip", AsyncMock(side_effect=side_effect)
    )
    with caplog.at_level("DEBUG", logger=_CONNECT_BACK_LOGGER):
        await rb_connect_back._dial_peer(receiver, peer, announce_port=6055)
    assert receiver.state.connect_back_cooldowns.strikes("alpha") == 1
    assert not receiver.state.connect_back_cooldowns.ready("alpha")
    assert any(snippet in record.message and record.levelname == level for record in caplog.records)


async def test_escalate_jitter_stays_under_cap(tmp_path: Path) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    for _ in range(10):
        rb_connect_back._escalate(receiver, "alpha")
        remaining = receiver.state.connect_back_cooldowns.remaining("alpha")
        assert remaining <= rb_connect_back._CONNECT_BACK_RETRY_CAP_SECONDS


# ---------------------------------------------------------------------------
# Later review-round regression pins (approve-path converge, teardown,
# backoff saturation, wire parsing)
# ---------------------------------------------------------------------------


async def test_pair_status_approve_converges_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, pairing = _offloader_with_pairing(tmp_path, status=PeerStatus.PENDING)
    order: list[str] = []
    controller.offloader._db.apply_remote_build_enabled = AsyncMock(
        side_effect=lambda: order.append("converge")
    )
    monkeypatch.setattr(
        controller.offloader, "_spawn_peer_link_client", lambda _p: order.append("spawn")
    )
    controller.offloader._db.bus = MagicMock()

    done = await rb_pair_status.apply_pair_status_result(
        controller.offloader,
        pairing,
        PairStatusResult(status=IntentResponse.APPROVED, pin_sha256=PIN),
    )
    assert done is True
    assert pairing.status is PeerStatus.APPROVED
    assert order == ["converge", "spawn"]


async def test_pairing_transition_event_triggers_converge() -> None:
    """A pairing status flip re-converges; shutdown unsubscribes."""
    event_type = EventType.OFFLOADER_PAIR_STATUS_CHANGED
    db = MagicMock()
    db.bus = EventBus()
    db.loop = asyncio.get_running_loop()
    db.create_background_task = asyncio.create_task
    lifecycle = RemoteBuildLifecycle(db)
    converge = AsyncMock()
    lifecycle.converge = converge  # type: ignore[method-assign]

    lifecycle.track_pairing_transitions()
    db.bus.fire(event_type, {})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert converge.await_count == 1

    await lifecycle.shutdown()
    db.bus.fire(event_type, {})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert converge.await_count == 1


async def test_remove_peer_clears_connect_back_state(tmp_path: Path) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    task = asyncio.create_task(asyncio.sleep(30))
    receiver.state.connect_back_tasks["alpha"] = task
    receiver.state.connect_back_cooldowns.escalate("alpha", 100.0, 1000.0)

    rb_connect_back.on_peer_removed(receiver, "alpha")

    with pytest.raises(asyncio.CancelledError):
        await task
    assert "alpha" not in receiver.state.connect_back_tasks
    assert "alpha" not in receiver.state.connect_back_last_contact
    assert "alpha" not in receiver.state.connect_back_cooldowns


async def test_commit_unchanged_endpoint_skips_rebound_event(tmp_path: Path) -> None:
    controller, pairing = _offloader_with_pairing(tmp_path)
    controller.offloader._db.bus = MagicMock()
    await rb_rebind.commit_endpoint_rebind(
        controller.offloader,
        pairing,
        hostname=pairing.receiver_hostname,
        port=pairing.receiver_port,
    )
    fired = [
        c
        for c in controller.offloader._db.bus.fire.call_args_list
        if c.args[0] is EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND
    ]
    assert fired == []


async def test_commit_changed_endpoint_fires_rebound_event(tmp_path: Path) -> None:
    controller, pairing = _offloader_with_pairing(tmp_path)
    controller.offloader._db.bus = MagicMock()
    await rb_rebind.commit_endpoint_rebind(controller.offloader, pairing, hostname=IP, port=6123)
    fired = [
        c
        for c in controller.offloader._db.bus.fire.call_args_list
        if c.args[0] is EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND
    ]
    assert len(fired) == 1
    assert fired[0].args[1]["receiver_hostname"] == IP


async def test_apply_reply_error_escalates_instead_of_hot_looping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, peer = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    monkeypatch.setattr(
        rb_connect_back,
        "drive_initiator_round_trip",
        AsyncMock(return_value=_round_trip("ok")),
    )
    monkeypatch.setattr(
        rb_connect_back, "_apply_reply", MagicMock(side_effect=RuntimeError("boom"))
    )
    await rb_connect_back._dial_peer(receiver, peer, announce_port=6055)
    assert receiver.state.connect_back_cooldowns.strikes("alpha") == 1


async def test_converge_helper_is_fail_soft(tmp_path: Path) -> None:
    controller, _ = _offloader_with_pairing(tmp_path)
    controller.offloader._db.apply_remote_build_enabled = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    await rb_peer_link_lifecycle.converge_listener_for_connect_back(controller.offloader)


async def test_unregister_after_removal_does_not_restamp(tmp_path: Path) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    receiver.state.approved_peers.clear()
    receiver.state.connect_back_last_contact.clear()
    rb_connect_back.on_session_unregistered(receiver, "alpha")
    assert "alpha" not in receiver.state.connect_back_last_contact


async def test_dial_failures_warn_once_per_streak_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    controller, peer = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    monkeypatch.setattr(
        rb_connect_back,
        "drive_initiator_round_trip",
        AsyncMock(side_effect=PeerLinkClientError("refused")),
    )
    with caplog.at_level("WARNING", logger=_CONNECT_BACK_LOGGER):
        for _ in range(rb_connect_back._CONNECT_BACK_WARN_AFTER_STRIKES + 2):
            await rb_connect_back._dial_peer(receiver, peer, announce_port=6055)
    warnings = [r for r in caplog.records if "keeps failing" in r.message]
    assert len(warnings) == 1


async def test_connect_back_delegate_routes_to_rebind(tmp_path: Path) -> None:
    controller = make_remote_build_controller(config_dir=tmp_path)
    outcome = await controller.offloader._handle_connect_back(
        pin_sha256=PIN, peer_ip=IP, announced_port=6055
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.NO_APPROVED_PEER)


async def test_loop_sweeps_and_survives_a_sweep_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _ = _receiver_ready_to_dial(tmp_path)
    calls: list[int] = []

    def _sweep(_controller: ReceiverController) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(rb_connect_back, "_CONNECT_BACK_SWEEP_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(rb_connect_back, "sweep_connect_back", _sweep)
    task = asyncio.create_task(rb_connect_back.run_connect_back_loop(controller.receiver))
    while len(calls) < 2:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_msg3_port_parses_and_logs_malformed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG", logger=_HANDSHAKE_LOGGER):
        assert _msg3_port({"connect_back_port": "nope"}, "connect_back_port", "192.0.2.1") == 0
        assert _msg3_port({}, "peer_link_port", "192.0.2.1") == 0
        assert _msg3_port({"peer_link_port": 6055}, "peer_link_port", "192.0.2.1") == 6055
    assert "malformed connect_back_port" in caplog.text
    assert "peer_link_port" not in caplog.text


def test_connect_back_port_getter_reads_listener_port(tmp_path: Path) -> None:
    controller = make_remote_build_controller(config_dir=tmp_path)
    controller.offloader._db.remote_build_listener_port = 6123
    assert rb_peer_link_lifecycle._connect_back_port_getter(controller.offloader)() == 6123


async def test_connect_back_session_opened_during_probe_skips_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forward session that lands mid-probe is not torn down; reply is already_connected."""
    controller, _ = _offloader_with_pairing(tmp_path)
    commit = AsyncMock()
    monkeypatch.setattr(controller.offloader, "_commit_endpoint_rebind", commit)

    async def _probe(**_kwargs: Any) -> RebindProbeResult:
        # Session opens while the probe is in flight.
        controller.offloader.state.open_peer_links.add(PIN)
        return RebindProbeResult(RebindProbeOutcome.OK)

    monkeypatch.setattr(controller.offloader, "_probe_pairing_endpoint", _probe)
    outcome = await rb_rebind.handle_connect_back(
        controller.offloader, pin_sha256=PIN, peer_ip=IP, announced_port=6123
    )
    assert outcome == IntentOutcome(IntentResponse.REJECTED, RejectReason.ALREADY_CONNECTED)
    commit.assert_not_awaited()


async def test_rebind_in_progress_escalates_after_repeats(tmp_path: Path) -> None:
    """A perpetual rebind_in_progress ramps its retry instead of a flat 30s forever."""
    controller, _ = _receiver_ready_to_dial(tmp_path)
    receiver = controller.receiver
    reply = _round_trip("rejected", RejectReason.REBIND_IN_PROGRESS.value)
    first = None
    for _ in range(4):
        rb_connect_back._apply_reply(receiver, "alpha", reply)
        remaining = receiver.state.connect_back_cooldowns.remaining("alpha")
        if first is None:
            first = remaining
    assert receiver.state.connect_back_cooldowns.strikes("alpha") == 4
    # First retry near the short base; later retries strictly longer.
    assert first <= rb_connect_back._CONNECT_BACK_SHORT_RETRY_SECONDS * 1.3
    assert remaining > first
    assert remaining <= rb_connect_back._CONNECT_BACK_RETRY_BASE_SECONDS
