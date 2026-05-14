"""
End-to-end: pair + long-lived peer-link session.

Smoke tests for the ``paired_instances`` harness — confirms the
two-controller bring-up reaches a state where both sides have
observed the peer-link session opening, before the
application-message phases (5b/5c/5d) build their own
assertions on top.
"""

from __future__ import annotations

import pytest
from esphome.const import __version__ as receiver_version

from .conftest import PairedInstances


@pytest.mark.asyncio
async def test_paired_instances_open_peer_link_session(
    paired_instances: PairedInstances,
) -> None:
    """The offloader's ``PeerLinkClient`` connects and both sides observe the session.

    Pins the harness contract: after ``paired_instances`` yields,
    :meth:`wait_until_session_opened` blocks until both
    ``OFFLOADER_PEER_LINK_OPENED`` (offloader-side) and
    ``RECEIVER_PEER_LINK_SESSION_OPENED`` (receiver-side) have
    fired. Tests assert against the harness's pre-rolled
    captured-event lists rather than re-subscribing after the
    fixture yields — by which point the events have already
    fired and a fresh listener would never see them.
    """
    await paired_instances.wait_until_session_opened()

    # Offloader fired OFFLOADER_PEER_LINK_OPENED with the
    # receiver coordinates the offloader dialled.
    assert len(paired_instances.offloader_opened) == 1
    assert paired_instances.offloader_opened[0]["receiver_hostname"] == "127.0.0.1"
    assert (
        paired_instances.offloader_opened[0]["receiver_port"]
        == paired_instances.receiver_server.port
    )

    # Receiver fired RECEIVER_PEER_LINK_SESSION_OPENED with the
    # offloader's dashboard_id, and the session is registered.
    assert len(paired_instances.receiver_opened) == 1
    assert (
        paired_instances.receiver_opened[0]["dashboard_id"]
        == paired_instances.offloader_dashboard_id
    )
    sessions = paired_instances.receiver._peer_link_sessions
    assert paired_instances.offloader_dashboard_id in sessions


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
    ``unregister_peer_link_session`` — which fires
    ``RECEIVER_PEER_LINK_SESSION_CLOSED`` and drops the row from
    ``_peer_link_sessions``.

    The fixture teardown runs ``offloader.stop → receiver.stop
    → server.close`` after this body returns; this body drives
    ``offloader.stop()`` explicitly so the cleanup contract can
    be observed from inside the test rather than relying on
    fixture teardown side-effects no test code sees.
    """
    await paired_instances.wait_until_session_opened()
    receiver_key = paired_instances.offloader_dashboard_id
    assert receiver_key in paired_instances.receiver._peer_link_sessions

    await paired_instances.offloader.stop()
    await paired_instances.wait_until_session_closed()

    # (a) Offloader's CLOSED carries the right reason.
    assert paired_instances.offloader_closed[0]["receiver_hostname"] == "127.0.0.1"
    assert (
        paired_instances.offloader_closed[0]["receiver_port"]
        == paired_instances.receiver_server.port
    )
    assert paired_instances.offloader_closed[0]["reason"] == "client_stopped"

    # (b) Offloader's registry drained synchronously by ``stop()``.
    assert paired_instances.offloader.state.peer_link_clients == {}

    # (c) Receiver's CLOSED carries the offloader's dashboard_id
    # and the registry has dropped the row.
    assert paired_instances.receiver_closed[0]["dashboard_id"] == receiver_key
    assert receiver_key not in paired_instances.receiver._peer_link_sessions

    # (d) Clean-stop close path doesn't populate ``error_detail``;
    # the category-level ``client_stopped`` reason already
    # explains the close, so a synthetic detail string would just
    # be noise on the operator's "last connection error" line.
    assert paired_instances.offloader_closed[0]["error_detail"] == ""


@pytest.mark.asyncio
async def test_paired_instances_snapshot_reflects_connected_state(
    paired_instances: PairedInstances,
) -> None:
    """``pairings_snapshot`` mirrors the live session's ``connecting`` / ``connected`` state.

    Pins the offloader-side projection contract: once the
    long-lived peer-link client has reached the post-handshake
    open state, the snapshot a freshly-subscribing frontend
    would read shows ``connected=True, connecting=False`` and an
    empty ``last_connect_error``. The same projection drives
    ``initial_state.pairings`` on every ``subscribe_events`` so
    a tab opening after the session is already up paints the
    right pill on first render rather than waiting on a
    follow-up event.
    """
    await paired_instances.wait_until_session_opened()
    [summary] = paired_instances.offloader.pairings_snapshot()
    assert summary.pin_sha256 == paired_instances.pin_sha256
    assert summary.connected is True
    assert summary.connecting is False
    assert summary.last_connect_error == ""


@pytest.mark.asyncio
async def test_paired_instances_snapshot_carries_receiver_esphome_version(
    paired_instances: PairedInstances,
) -> None:
    """``pairings_snapshot`` surfaces the receiver's esphome_version after session-open.

    Pins the wire-and-capture round-trip that unblocks
    pick_build_path's deferred version-compat gate:

    1. Receiver's ``_send_response`` ships its bundled
       :data:`esphome.const.__version__` on every
       ``intent_response`` payload.
    2. Offloader's :meth:`PeerLinkClient._run_one_session`
       lifts the field off the decoded response.
    3. ``_fire_opened`` rides it onto
       ``OFFLOADER_PEER_LINK_OPENED``.
    4. The controller's listener writes it onto the matching
       :class:`StoredPairing`.
    5. :meth:`pairings_snapshot` projects it onto
       :attr:`PairingSummary.esphome_version`.

    Both halves run the same ``esphome`` package (single
    process), so the value the offloader observes on its
    snapshot equals the version the receiver imported — pin
    that here so a regression on the wire-shape, the lift,
    the listener wiring, or the projection trips this test
    rather than producing a silent empty value on
    pick_build_path's gate input.
    """
    await paired_instances.wait_until_session_opened()
    [summary] = paired_instances.offloader.pairings_snapshot()
    assert summary.esphome_version == receiver_version
