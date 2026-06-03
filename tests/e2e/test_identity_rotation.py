"""
End-to-end: offloader peer-link identity rotation + recovery.

Rotate the offloader's key over the real two-instance wire, watch
the session drop, re-pair, and confirm it recovers pinned to the
rotated key.
"""

from __future__ import annotations

import asyncio

from esphome_device_builder.helpers.peer_link_identity import PeerLinkIdentityStore
from esphome_device_builder.models import EventType, RemoteBuildIdentityRotatedData

from ..conftest import capture_events
from .conftest import PairedInstances


async def _repair(instances: PairedInstances) -> None:
    """Run the receiver-window → preview → request → approve re-pair sequence."""
    port = instances.receiver_server.port
    assert port is not None  # TestServer always binds; narrow for type-checkers.
    await instances.receiver.set_pairing_window(open=True, client="receiver-tab")
    preview = await instances.offloader.preview_pair(hostname="127.0.0.1", port=port)
    await instances.offloader.request_pair(
        hostname="127.0.0.1",
        port=port,
        pin_sha256=preview["pin_sha256"],
        receiver_label="receiver",
        offloader_label="offloader",
    )
    pair_status_changed = capture_events(
        instances.offloader_bus, EventType.OFFLOADER_PAIR_STATUS_CHANGED
    )
    await instances.receiver.approve_peer(dashboard_id=instances.offloader_dashboard_id)
    await asyncio.wait_for(pair_status_changed.received.wait(), timeout=2.0)


async def test_offloader_identity_rotation_recovers_session(
    paired_instances: PairedInstances,
) -> None:
    """A rotated offloader identity drops the link; re-pair recovers it under the new key."""
    await paired_instances.wait_until_session_opened()
    dashboard_id = paired_instances.offloader_dashboard_id
    receiver_pin = paired_instances.pin_sha256
    v0_pin = paired_instances.receiver.state.approved_peers[dashboard_id].pin_sha256

    # Rotate the offloader's peer-link key, then announce it on the
    # offloader bus exactly as ``rotate_identity`` does.
    store: PeerLinkIdentityStore = paired_instances.offloader._db.peer_link_identity_store
    v1 = await store.async_rotate()
    assert v1.pin_sha256 != v0_pin

    reopened = capture_events(
        paired_instances.receiver_bus, EventType.RECEIVER_PEER_LINK_SESSION_OPENED
    )
    paired_instances.offloader_bus.fire(
        EventType.REMOTE_BUILD_IDENTITY_ROTATED,
        RemoteBuildIdentityRotatedData(dashboard_id=dashboard_id, pin_sha256=v1.pin_sha256),
    )

    # The respawned client presents the new key; the receiver still
    # pins v0, so the old session drops and the rotated identity is
    # rejected until the operator re-pairs.
    await paired_instances.wait_until_session_closed()

    # Operator removes the stale peer and re-pairs the rotated identity.
    await paired_instances.receiver.remove_peer(dashboard_id=dashboard_id)
    await _repair(paired_instances)

    # Session recovers, now pinned to the rotated key on both sides.
    await asyncio.wait_for(reopened.received.wait(), timeout=2.0)
    assert dashboard_id in paired_instances.receiver.state.peer_link_sessions
    assert paired_instances.receiver.state.approved_peers[dashboard_id].pin_sha256 == v1.pin_sha256
    live = paired_instances.offloader.state.peer_link_clients[receiver_pin]
    assert live.client._identity_pub == v1.public_bytes
