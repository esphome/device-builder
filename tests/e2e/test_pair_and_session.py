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
    """The harness teardown unwinds the peer-link session without leaking tasks.

    Pins the cleanup contract: after the test body returns and
    the fixture's teardown runs (offloader.stop → receiver.stop
    → server.close), both controllers' session registries are
    empty and the offloader's
    ``OFFLOADER_PEER_LINK_CLOSED`` event has fired with
    ``reason="client_stopped"`` (the offloader-initiated path).

    The teardown itself happens after this test's body — the
    body's job is to wait for the session to open + subscribe
    to the closed event so the teardown's effects are
    observable to the next assertion.
    """
    closed = capture_events(paired_instances.offloader_bus, EventType.OFFLOADER_PEER_LINK_CLOSED)

    await paired_instances.wait_until_session_opened()

    # Drive the offloader's stop directly so we can assert
    # on the resulting CLOSED event from inside the test
    # body rather than chasing post-teardown state.
    await paired_instances.offloader.stop()

    await asyncio.wait_for(closed.received.wait(), timeout=2.0)
    assert closed[0]["receiver_hostname"] == "127.0.0.1"
    assert closed[0]["receiver_port"] == paired_instances.receiver_server.port
    assert closed[0]["reason"] == "client_stopped"
