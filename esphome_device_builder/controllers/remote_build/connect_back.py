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
# Strike count at which the backoff saturates the cap; the streak's
# one operator-visible warning fires there.
_CONNECT_BACK_WARN_AFTER_STRIKES = 5


def on_session_registered(controller: ReceiverController, dashboard_id: str) -> None:
    """Reset *dashboard_id*'s quiet clock; cancel its in-flight dial and cooldown."""
    _cancel_dial(controller, dashboard_id)
    controller.state.connect_back_last_contact[dashboard_id] = time.monotonic()


def on_session_unregistered(controller: ReceiverController, dashboard_id: str) -> None:
    """Start *dashboard_id*'s quiet clock at session close; skip removed peers."""
    if dashboard_id in controller.state.approved_peers:
        controller.state.connect_back_last_contact[dashboard_id] = time.monotonic()


def on_peer_removed(controller: ReceiverController, dashboard_id: str) -> None:
    """Cancel any in-flight dial and drop *dashboard_id*'s connect-back state."""
    _cancel_dial(controller, dashboard_id)
    controller.state.connect_back_last_contact.pop(dashboard_id, None)


def clear_state(controller: ReceiverController) -> None:
    """Drop every connect-back task handle, quiet stamp, and cooldown."""
    state = controller.state
    state.connect_back_tasks.clear()
    state.connect_back_last_contact.clear()
    state.connect_back_cooldowns.clear()


async def run_connect_back_loop(controller: ReceiverController) -> None:
    """Periodically dial back quiet paired offloaders; runs until cancelled."""
    while True:
        await asyncio.sleep(_CONNECT_BACK_SWEEP_INTERVAL_SECONDS)
        try:
            sweep_connect_back(controller)
        except Exception:
            _LOGGER.exception("connect-back sweep failed; continuing")


def sweep_connect_back(controller: ReceiverController) -> None:
    """Spawn one dial task per eligible quiet peer."""
    db = controller._db
    announce_port = db.remote_build_listener_port
    if announce_port is None or not db.remote_build_receiver_role_active:
        return
    state = controller.state
    now = time.monotonic()
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
    _LOGGER.info(
        "connect-back dialing offloader %s at %s:%d",
        dashboard_id,
        peer.peer_ip,
        peer.connect_back_port,
    )
    try:
        identity = await controller._db.peer_link_identity_store.async_load()
        round_trip = await drive_initiator_round_trip(
            hostname=peer.peer_ip,
            port=peer.connect_back_port,
            identity_priv=identity.private_bytes,
            intent=PeerLinkIntent.CONNECT_BACK,
            msg3_payload=_json.dumps({"peer_link_port": announce_port}),
            timeout_seconds=_CONNECT_BACK_DIAL_TIMEOUT_SECONDS,
            expected_pin_sha256=peer.pin_sha256,
        )
        _apply_reply(controller, dashboard_id, round_trip)
    except PeerLinkPinMismatchError as exc:
        # A different static key answers at the last-known endpoint —
        # spoof, hijack, or offloader key rotation.
        _LOGGER.warning(
            "connect-back dial to %s: static key mismatch at %s:%d: %s",
            dashboard_id,
            peer.peer_ip,
            peer.connect_back_port,
            exc,
        )
        _escalate(controller, dashboard_id)
    except PeerLinkClientError as exc:
        _LOGGER.debug("connect-back dial to %s failed: %s", dashboard_id, exc)
        _escalate(controller, dashboard_id)
    except Exception:
        _LOGGER.exception("connect-back dial to %s failed unexpectedly", dashboard_id)
        _escalate(controller, dashboard_id)


def _apply_reply(
    controller: ReceiverController, dashboard_id: str, round_trip: InitiatorRoundTrip
) -> None:
    """Map the offloader's reply onto the per-peer retry backoff."""
    state = controller.state
    if round_trip.intent_response == IntentResponse.OK.value:
        _LOGGER.info("connect-back announce accepted by offloader %s", dashboard_id)
        state.connect_back_cooldowns.discard(dashboard_id)
        state.connect_back_last_contact[dashboard_id] = time.monotonic()
        return
    reason = round_trip.response.get("reason")
    if reason == RejectReason.REBIND_IN_PROGRESS.value:
        # Normally clears within one 30s cooldown; ramp toward the
        # base retry so a slot stuck warm forever doesn't re-dial
        # every 30s in silence.
        level = logging.DEBUG
        _escalate(
            controller,
            dashboard_id,
            base=_CONNECT_BACK_SHORT_RETRY_SECONDS,
            cap=_CONNECT_BACK_RETRY_BASE_SECONDS,
        )
    elif reason == RejectReason.ALREADY_CONNECTED.value:
        level = logging.DEBUG
        _escalate(controller, dashboard_id)
    elif reason == RejectReason.PROBE_FAILED.value:
        # The offloader could not verify us back — asymmetric
        # reachability; automatic recovery can't complete.
        level = logging.WARNING
        _escalate(controller, dashboard_id)
    else:
        # bad_intent (older offloader) / no_approved_peer /
        # bad_endpoint / unknown — unlikely to recover soon; park at
        # cap and keep retrying so a later re-pair still self-heals.
        level = logging.WARNING
        state.connect_back_cooldowns.set(dashboard_id, _CONNECT_BACK_RETRY_CAP_SECONDS)
    _LOGGER.log(level, "connect-back announce refused by %s (reason=%s)", dashboard_id, reason)


def _escalate(
    controller: ReceiverController,
    dashboard_id: str,
    *,
    base: float = _CONNECT_BACK_RETRY_BASE_SECONDS,
    cap: float = _CONNECT_BACK_RETRY_CAP_SECONDS,
) -> None:
    """Escalate *dashboard_id*'s retry cooldown; warn once when the streak saturates."""
    cooldowns = controller.state.connect_back_cooldowns
    cooldowns.escalate(
        dashboard_id,
        base * random.uniform(1 - _CONNECT_BACK_JITTER, 1 + _CONNECT_BACK_JITTER),  # noqa: S311 — jitter, not crypto
        cap,
    )
    if cooldowns.strikes(dashboard_id) == _CONNECT_BACK_WARN_AFTER_STRIKES:
        _LOGGER.warning(
            "connect-back to %s keeps failing; retrying at the backoff cap", dashboard_id
        )


def _cancel_dial(controller: ReceiverController, dashboard_id: str) -> None:
    """Cancel *dashboard_id*'s in-flight dial and drop its cooldown."""
    state = controller.state
    if (task := state.connect_back_tasks.pop(dashboard_id, None)) is not None:
        task.cancel()
    state.connect_back_cooldowns.discard(dashboard_id)


def _pop_dial_task(controller: ReceiverController, dashboard_id: str, task: asyncio.Task) -> None:
    """Drop *task*'s registry slot iff it still owns it."""
    if controller.state.connect_back_tasks.get(dashboard_id) is task:
        del controller.state.connect_back_tasks[dashboard_id]
