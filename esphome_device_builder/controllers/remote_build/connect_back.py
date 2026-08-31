"""
Receiver-side connect-back dialing for quiet paired offloaders.

When an offloader hasn't reached us for a while (its stored
endpoint for us went stale — IP change across subnets, no mDNS),
dial its last-known IP + announced listener port with
``intent="connect_back"`` and announce our peer-link port; the
offloader probes and rebinds, then reconnects forward. Bodies
take :class:`ReceiverController` as the first arg.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from functools import partial
from typing import TYPE_CHECKING

from ...helpers import json as _json
from ...models import IntentResponse, PeerLinkIntent, RejectReason, StoredPeer
from .peer_link_client import PeerLinkClientError, PeerLinkPinMismatchError
from .peer_link_client.one_shot import drive_initiator_round_trip

if TYPE_CHECKING:
    from ._client_models import InitiatorRoundTrip
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

_CONNECT_BACK_SWEEP_INTERVAL_SECONDS = 30.0
# Well above the forward client's 30s max reconnect backoff so a
# reachable offloader always wins the race to reconnect first.
_CONNECT_BACK_QUIET_SECONDS = 300.0
_CONNECT_BACK_RETRY_BASE_SECONDS = 300.0
_CONNECT_BACK_RETRY_CAP_SECONDS = 3600.0
_CONNECT_BACK_SHORT_RETRY_SECONDS = 30.0
# Covers the offloader's inline forward probe before it replies.
_CONNECT_BACK_DIAL_TIMEOUT_SECONDS = 30.0
_CONNECT_BACK_JITTER = 0.2


def on_session_registered(controller: ReceiverController, dashboard_id: str) -> None:
    """Reset *dashboard_id*'s quiet clock; cancel its in-flight dial and cooldown."""
    state = controller.state
    state.connect_back_last_contact[dashboard_id] = _monotonic()
    if (task := state.connect_back_tasks.pop(dashboard_id, None)) is not None:
        task.cancel()
    state.connect_back_cooldowns.prune(lambda k: k != dashboard_id)


def on_session_unregistered(controller: ReceiverController, dashboard_id: str) -> None:
    """Start *dashboard_id*'s quiet clock at session close."""
    controller.state.connect_back_last_contact[dashboard_id] = _monotonic()


async def run_connect_back_loop(controller: ReceiverController) -> None:
    """Periodically dial back quiet paired offloaders; runs until cancelled."""
    while True:
        await asyncio.sleep(_CONNECT_BACK_SWEEP_INTERVAL_SECONDS)
        sweep_connect_back(controller)


def sweep_connect_back(controller: ReceiverController) -> None:
    """Spawn one dial task per eligible quiet peer."""
    db = controller._db
    announce_port = db.remote_build_listener_port
    if announce_port is None or not db.remote_build_receiver_role_active:
        return
    state = controller.state
    now = _monotonic()
    for dashboard_id, peer in state.approved_peers.items():
        # A peer first seen now waits one full quiet window.
        last_contact = state.connect_back_last_contact.setdefault(dashboard_id, now)
        if (
            peer.connect_back_port <= 0
            or not peer.peer_ip
            or dashboard_id in state.peer_link_sessions
            or dashboard_id in state.connect_back_tasks
            or now - last_contact < _CONNECT_BACK_QUIET_SECONDS
            or not state.connect_back_cooldowns.ready(dashboard_id, now)
        ):
            continue
        task = controller._track_task(
            _dial_peer(controller, peer, announce_port=announce_port),
            name=f"connect-back-{dashboard_id}",
        )
        state.connect_back_tasks[dashboard_id] = task
        task.add_done_callback(partial(_pop_dial_task, controller, dashboard_id))


async def _dial_peer(
    controller: ReceiverController, peer: StoredPeer, *, announce_port: int
) -> None:
    """Dial *peer*'s last-known endpoint and announce our listener port."""
    dashboard_id = peer.dashboard_id
    identity = await controller._db.peer_link_identity_store.async_load()
    _LOGGER.info(
        "connect-back dialing offloader %s at %s:%d",
        dashboard_id,
        peer.peer_ip,
        peer.connect_back_port,
    )
    try:
        round_trip = await drive_initiator_round_trip(
            hostname=peer.peer_ip,
            port=peer.connect_back_port,
            identity_priv=identity.private_bytes,
            intent=PeerLinkIntent.CONNECT_BACK,
            msg3_payload=_json.dumps({"peer_link_port": announce_port}),
            timeout_seconds=_CONNECT_BACK_DIAL_TIMEOUT_SECONDS,
            expected_pin_sha256=peer.pin_sha256,
        )
    except (PeerLinkClientError, PeerLinkPinMismatchError) as exc:
        _LOGGER.debug("connect-back dial to %s failed: %s", dashboard_id, exc)
        _escalate(controller, dashboard_id)
        return
    _apply_reply(controller, dashboard_id, round_trip)


def _apply_reply(
    controller: ReceiverController, dashboard_id: str, round_trip: InitiatorRoundTrip
) -> None:
    """Map the offloader's reply onto the per-peer retry backoff."""
    state = controller.state
    if round_trip.intent_response == IntentResponse.OK.value:
        _LOGGER.info("connect-back announce accepted by offloader %s", dashboard_id)
        state.connect_back_cooldowns.prune(lambda k: k != dashboard_id)
        state.connect_back_last_contact[dashboard_id] = _monotonic()
        return
    reason = round_trip.response.get("reason")
    _LOGGER.debug("connect-back announce refused by %s (reason=%s)", dashboard_id, reason)
    if reason == RejectReason.REBIND_IN_PROGRESS.value:
        state.connect_back_cooldowns.set(dashboard_id, _CONNECT_BACK_SHORT_RETRY_SECONDS)
        return
    if reason in (RejectReason.ALREADY_CONNECTED.value, RejectReason.PROBE_FAILED.value):
        _escalate(controller, dashboard_id)
        return
    # bad_intent (older offloader) / no_approved_peer / bad_endpoint
    # / unknown — won't recover this process lifetime; park at cap.
    state.connect_back_cooldowns.set(dashboard_id, _CONNECT_BACK_RETRY_CAP_SECONDS)


def _escalate(controller: ReceiverController, dashboard_id: str) -> None:
    """Escalate *dashboard_id*'s retry cooldown with jittered exponential backoff."""
    controller.state.connect_back_cooldowns.escalate(
        dashboard_id,
        _CONNECT_BACK_RETRY_BASE_SECONDS
        * random.uniform(  # noqa: S311 — jitter, not crypto
            1 - _CONNECT_BACK_JITTER, 1 + _CONNECT_BACK_JITTER
        ),
        _CONNECT_BACK_RETRY_CAP_SECONDS,
    )


def _pop_dial_task(controller: ReceiverController, dashboard_id: str, task: asyncio.Task) -> None:
    """Drop *task*'s registry slot iff it still owns it."""
    if controller.state.connect_back_tasks.get(dashboard_id) is task:
        del controller.state.connect_back_tasks[dashboard_id]


def _monotonic() -> float:
    """Indirection so tests can monkey-patch the quiet clock."""
    return time.monotonic()
