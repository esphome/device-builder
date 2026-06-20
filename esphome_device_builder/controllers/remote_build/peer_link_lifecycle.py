"""
Offloader-side peer-link client lifecycle.

Owns spawn / cancel / lookup of the long-lived
:class:`PeerLinkClient` task per APPROVED
:class:`StoredPairing`, plus the
:func:`sweep_stale_pairings_at_endpoint` helper that
re-pair / endpoint-take-over uses to drop orphaned rows
sharing a hostname / port. Bodies take
:class:`OffloaderController` as the first arg; the
controller keeps thin bound-method delegates so tests can
instance-patch the spawn / cancel hooks (the dominant
pattern across the controller-test suite) and so cross-module
callers (``rebind`` for cancel/respawn,
``submit_job`` / ``download_artifacts`` / ``cancel_job`` for
lookup) reach into a stable surface.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import ErrorCode, PeerStatus, StoredPairing
from ._models import PeerLinkClientHandle
from ._shared import drain_tasks
from .peer_link_client import PeerLinkClient

if TYPE_CHECKING:
    from .offloader import OffloaderController


def spawn_peer_link_client(controller: OffloaderController, pairing: StoredPairing) -> None:
    """Spawn the long-lived peer-link client for *pairing*.

    Idempotent on already-running clients — returns early if
    a client for the row's ``pin_sha256`` is still alive.
    Skips if the offloader-side identities haven't been
    loaded yet (start order: identities load before any
    spawn). Skips if the bus isn't wired (e.g. a unit test
    path).
    """
    if (
        controller.state.offloader_dashboard_id is None
        or controller.state.offloader_peer_link_priv is None
        or controller._db.bus is None
    ):
        return
    key = pairing.pin_sha256
    existing = controller.state.peer_link_clients.get(key)
    if existing is not None and not existing.task.done():
        return
    client = PeerLinkClient(
        receiver_hostname=pairing.receiver_hostname,
        receiver_port=pairing.receiver_port,
        identity_priv=controller.state.offloader_peer_link_priv,
        dashboard_id=controller.state.offloader_dashboard_id,
        # Pin the receiver's static pubkey from the
        # OOB-verified pair flow so the long-lived peer-link
        # handshake fails fast on identity drift instead of
        # admitting an attacker with their own keypair to
        # the application channel.
        pinned_static_x25519_pub=pairing.static_x25519_pub,
        pin_sha256=pairing.pin_sha256,
        receiver_label=pairing.label,
        bus=controller._db.bus,
        resolver=controller.state.peer_link_resolver,
    )
    task = asyncio.create_task(
        client.run(),
        name=f"peer-link-client-{pairing.receiver_hostname}:{pairing.receiver_port}",
    )
    controller.state.peer_link_clients[key] = PeerLinkClientHandle(client=client, task=task)


def cancel_peer_link_client(controller: OffloaderController, pin_sha256: str) -> None:
    """Cancel the peer-link client for *pin_sha256*. No-op if none running."""
    handle = controller.state.peer_link_clients.pop(pin_sha256, None)
    if handle is not None and not handle.task.done():
        handle.task.cancel()


async def cancel_peer_link_client_and_wait(
    controller: OffloaderController, pin_sha256: str
) -> None:
    """Cancel the peer-link client for *pin_sha256* and await its teardown.

    The rebind respawn must let the old client's in-flight connect fully
    unwind (its ``terminate`` sent, the receiver's ``dashboard_id`` slot
    freed) before a new client connects; otherwise the late old
    registration supersedes the fresh client and orphans it.
    """
    handle = controller.state.peer_link_clients.pop(pin_sha256, None)
    if handle is None or handle.task.done():
        return
    # ``drain_tasks`` cancels + awaits and retrieves the outcome via
    # ``gather(return_exceptions=True)``: a teardown exception is logged and
    # absorbed (not "never retrieved") so it can't abort the caller's respawn,
    # while a cancellation of *this* task still propagates.
    await drain_tasks((handle.task,), log_exceptions=True)


def lookup_open_peer_link_client(
    controller: OffloaderController, pin_sha256: str, *, label: str
) -> PeerLinkClient:
    """Return the live :class:`PeerLinkClient` for *pin_sha256*, raising on miss.

    ``NOT_FOUND`` for a missing pairing; ``PRECONDITION_FAILED``
    for any of the not-ready states (PENDING, client not
    spawned, orphaned, mid-reconnect) — all four fold into
    one raise since the user's recovery is the same (wait +
    retry); the distinguishing reason rides in the log line.
    *label* names the calling op in the error message.
    """
    pairing = controller.state.pairings.get(pin_sha256)
    if pairing is None:
        msg = f"{label}: no pairing for pin_sha256={pin_sha256!r}"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    if pairing.status is not PeerStatus.APPROVED:
        reason = f"status is {pairing.status.value!r}, not APPROVED"
    elif (handle := controller.state.peer_link_clients.get(pin_sha256)) is None:
        reason = "client not yet spawned"
    elif handle.task.done():
        reason = "client orphaned (pin mismatch / superseded)"
    elif not handle.client.is_session_open:
        reason = "session not connected (mid-reconnect / receiver offline)"
    else:
        return handle.client
    msg = f"{label}: peer-link to {pairing.label!r} not ready ({reason})"
    raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)


def sweep_stale_pairings_at_endpoint(
    controller: OffloaderController, hostname: str, port: int, *, keep_pin_sha256: str
) -> None:
    """Drop any pairing or alert at ``(hostname, port)`` whose pin isn't *keep_pin_sha256*.

    Cleans up after a re-pair against the same endpoint
    under a fresh pin (receiver rotated identity, or a
    different receiver took the hostname). Without the
    sweep the old row + listener task + alert would leak
    under pin-keying.

    Walks both ``_pairings`` and ``_offloader_alerts``
    because an alert can outlive its pairing on the
    pin-drift branch. Snapshots to lists before iterating
    to avoid mutate-during-iteration. Each dropped pairing
    fires ``OFFLOADER_PAIR_STATUS_CHANGED`` ``"removed"`` and
    clears its derived caches.
    """
    for stale_pin, pairing in list(controller.state.pairings.items()):
        if stale_pin == keep_pin_sha256:
            continue
        if pairing.receiver_hostname != hostname or pairing.receiver_port != port:
            continue
        controller.state.pairings.pop(stale_pin, None)
        controller._cancel_pair_status_listener(stale_pin)
        controller._cancel_peer_link_client(stale_pin)
        controller.state.peer_queue_status.pop(stale_pin, None)
        for job_id, entry in list(controller.state.offloader_remote_jobs.items()):
            if entry["pin_sha256"] == stale_pin:
                controller.state.offloader_remote_jobs.pop(job_id, None)
        controller.state.open_peer_links.discard(stale_pin)
        # Fire "removed" so connected clients drop the row from
        # their pairings list — without it a swept (possibly
        # APPROVED) row lingers in every subscriber's snapshot
        # until reload (cross-tab desync).
        controller._fire_offloader_pair_status_changed(
            pairing.receiver_hostname, pairing.receiver_port, stale_pin, "removed"
        )
    # Alerts can outlive pairings — sweep them in a second
    # pass keyed on the alert's stored ``receiver_hostname``
    # / ``receiver_port`` (also walks the pin-keyed dict so
    # an alert under the keep_pin_sha256 stays put if the
    # user is re-confirming the same identity).
    for stale_pin, alert in list(controller.state.offloader_alerts.items()):
        if stale_pin == keep_pin_sha256:
            continue
        if alert["receiver_hostname"] != hostname or alert["receiver_port"] != port:
            continue
        controller._dismiss_offloader_alert(stale_pin, hostname, port)
