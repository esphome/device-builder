"""
End-to-end: pair + long-lived peer-link session.

Smoke tests for the ``paired_instances`` harness — confirms the
two-controller bring-up reaches a state where both sides have
observed the peer-link session opening, before the
application-message phases (5b/5c/5d) build their own
assertions on top.
"""

from __future__ import annotations

import asyncio

import pytest

from esphome_device_builder.models import EventType

from ..conftest import capture_events
from .conftest import PairedInstances


@pytest.mark.asyncio
async def test_paired_instances_open_peer_link_session(
    paired_instances: PairedInstances,
) -> None:
    """The offloader's ``PeerLinkClient`` connects and both sides observe the session.

    Pins the harness contract: after ``paired_instances`` yields,
    waiting on :meth:`wait_until_session_opened` is enough to
    have both the offloader-side ``OFFLOADER_PEER_LINK_OPENED``
    event fired and the receiver-side
    ``_peer_link_sessions[<dashboard_id>]`` registered.
    """
    opened = capture_events(paired_instances.offloader_bus, EventType.OFFLOADER_PEER_LINK_OPENED)

    await paired_instances.wait_until_session_opened()

    # Offloader fired OFFLOADER_PEER_LINK_OPENED with the
    # receiver coordinates the offloader dialled.
    await asyncio.wait_for(opened.received.wait(), timeout=2.0)
    assert len(opened) == 1
    assert opened[0]["receiver_hostname"] == "127.0.0.1"
    assert opened[0]["receiver_port"] == paired_instances.receiver_server.port

    # Receiver registered the offloader's session under the
    # offloader's dashboard_id.
    sessions = paired_instances.receiver._peer_link_sessions
    assert paired_instances.offloader_dashboard_id in sessions
    session = sessions[paired_instances.offloader_dashboard_id]
    assert session.dashboard_id == paired_instances.offloader_dashboard_id


@pytest.mark.asyncio
async def test_paired_instances_teardown_closes_session_cleanly(
    paired_instances: PairedInstances,
) -> None:
    """``offloader.stop()`` unwinds the peer-link session on both sides.

    Pins the cleanup contract: cancelling the offloader's
    long-lived peer-link client task (a) fires
    ``OFFLOADER_PEER_LINK_CLOSED`` with ``reason="client_stopped"``
    on the offloader-side bus, (b) drains the offloader's
    ``_peer_link_clients`` registry, and (c) lets the receiver's
    ``_run_peer_link_session`` finally-block run
    ``unregister_peer_link_session`` so the receiver's
    ``_peer_link_sessions`` registry drops the row.

    The fixture teardown runs ``offloader.stop → receiver.stop
    → server.close`` after this body returns; this body drives
    ``offloader.stop()`` explicitly so the cleanup contract can
    be observed from inside the test rather than relying on
    fixture teardown side-effects no test code sees.
    """
    closed = capture_events(paired_instances.offloader_bus, EventType.OFFLOADER_PEER_LINK_CLOSED)

    await paired_instances.wait_until_session_opened()
    receiver_key = paired_instances.offloader_dashboard_id
    assert receiver_key in paired_instances.receiver._peer_link_sessions

    await paired_instances.offloader.stop()

    # (a) CLOSED fires offloader-side with the right reason.
    await asyncio.wait_for(closed.received.wait(), timeout=2.0)
    assert closed[0]["receiver_hostname"] == "127.0.0.1"
    assert closed[0]["receiver_port"] == paired_instances.receiver_server.port
    assert closed[0]["reason"] == "client_stopped"

    # (b) Offloader's registry drained synchronously by ``stop()``.
    assert paired_instances.offloader._peer_link_clients == {}

    # (c) Receiver's session loop unwinds on its own task — the
    # offloader's CancelledError handler sent a structured
    # ``terminate{client_stopped}`` frame, the receiver's
    # ``_receive_loop`` exits, and ``unregister_peer_link_session``
    # runs in its ``finally``. There's no bus event for the
    # unregistration today, so wait_for + a short spin against
    # the registry is the available sync source.
    async def _registry_drained() -> None:
        while receiver_key in paired_instances.receiver._peer_link_sessions:
            await asyncio.sleep(0)

    await asyncio.wait_for(_registry_drained(), timeout=2.0)
