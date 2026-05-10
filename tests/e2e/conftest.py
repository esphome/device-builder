"""
End-to-end harness for the remote-build offload feature (issue #106).

Two real :class:`RemoteBuildController` instances stood up
side-by-side — one acting as the receiver (peer-link listener
bound on a real TCP port via :class:`aiohttp.test_utils.TestServer`),
one acting as the offloader (long-lived
:class:`PeerLinkClient` connecting to the receiver). Both run on
real :class:`EventBus` instances so per-mutation events flow
through the same wire surface a production frontend would
subscribe to.

Tests built on top of this harness exercise behaviour that
spans both sides of the wire — handshake → pair → peer-link
session → application messages (5b/5c/5d) → bundle upload +
firmware download (later phases). Single-side unit tests in
``test_remote_build_peer_link.py`` /
``test_remote_build_peer_link_client.py`` already pin the
per-side wire shapes; the harness's value is catching mismatches
between the two (event payload contracts, dashboard_id collisions,
terminate flow with both sides observing).

The harness drives the real pair flow end-to-end (no
dict-mocking shortcuts): receiver opens its pairing window,
offloader runs ``preview_pair`` + ``request_pair`` over real
Noise XX handshakes, receiver calls ``approve_peer``, then
the offloader's pair-status listener observes the flip and
spawns the long-lived peer-link client. Tests built on top of
``paired_instances`` start from "both sides have an APPROVED
row, the long-lived peer-link session is open, ready for
application messages."
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build_peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import EventType

from ..conftest import _CapturedEvents, capture_events, make_remote_build_controller


@dataclass
class PairedInstances:
    """Two controllers + a TestServer, pre-paired and ready to drive.

    Test code reads :attr:`offloader` / :attr:`receiver` to drive
    WS commands or assert on RAM-canonical state, and
    :attr:`offloader_dashboard_id` to look up the offloader's
    session on the receiver side
    (``receiver._peer_link_sessions[<offloader_dashboard_id>]``).

    :meth:`wait_until_session_opened` is the single conventional
    sync point; tests that need to assert on post-session state
    call it before their assertions instead of polling the
    registry by hand.
    """

    receiver: RemoteBuildController
    offloader: RemoteBuildController
    receiver_server: TestServer
    receiver_bus: EventBus
    offloader_bus: EventBus
    offloader_dashboard_id: str
    # Pre-subscribed at fixture-construct time so a
    # :meth:`wait_until_session_opened` call lands cleanly even
    # when the OPENED event has already fired (the offloader's
    # :class:`PeerLinkClient` connects on its own task; tests
    # that race the listener subscription against the connect
    # would otherwise have to wire the subscription before the
    # fixture yields).
    _opened: _CapturedEvents

    async def wait_until_session_opened(self, *, timeout: float = 2.0) -> None:
        """Block until the offloader observes ``OFFLOADER_PEER_LINK_OPENED``.

        Event-based — the offloader fires
        ``OFFLOADER_PEER_LINK_OPENED`` from its
        :class:`PeerLinkClient` after processing the receiver's
        post-handshake ``intent_response: ok``, which is the
        offloader-side completion of the long-lived peer-link
        session bring-up. Tests that need to assert on
        receiver-side state (``_peer_link_sessions[<dashboard_id>]``)
        can layer their own short
        :func:`asyncio.wait_for` on top — the receiver's
        ``register_peer_link_session`` runs on its own task
        and may lag the offloader's OPENED fire by a tick.
        """
        await asyncio.wait_for(self._opened.received.wait(), timeout=timeout)


@pytest.fixture
async def paired_instances(
    tmp_path: Path,
) -> AsyncGenerator[PairedInstances, None]:
    """Yield two :class:`RemoteBuildController` instances paired via the real flow.

    Drives the production pair sequence end-to-end against two
    in-process controllers — no dict-mocking shortcuts:

    1. Both controllers ``start()`` (loads identities,
       installs the long-poll listener slot for any future
       PENDING rows, etc.).
    2. Receiver opens its pairing window
       (``set_pairing_window(open=True)``).
    3. Offloader runs ``preview_pair`` over a real Noise XX WS
       to capture the receiver's pubkey + pin from the
       handshake transcript.
    4. Offloader runs ``request_pair`` (also a real Noise WS)
       carrying the offloader's ``dashboard_id``; receiver's
       handler creates a PENDING :class:`StoredPeer` row and
       fires ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.
    5. Receiver runs ``approve_peer`` to flip PENDING →
       APPROVED; fires ``REMOTE_BUILD_PAIR_STATUS_CHANGED``.
    6. Offloader's pair-status listener (spawned in step 4)
       observes the flip via its long-poll WS, updates the
       local :class:`StoredPairing` to APPROVED, and spawns
       the long-lived :class:`PeerLinkClient` (5a-2).

    Per-side event buses are real, so production-shape event
    fan-out runs end-to-end. The handshake reads pin + dashboard_id
    from the live Noise transcript, so any wire-shape regression
    on either side surfaces here rather than being hidden behind
    a pre-seeded RAM dict.

    Teardown drains both controllers in dependency order:
    offloader first (its client task sends a
    ``terminate{client_stopped}`` to the receiver, the
    receiver's session loop unwinds), then the receiver (closing
    any remaining server-side state), then the TestServer.
    """
    receiver_dir = tmp_path / "receiver"
    receiver_dir.mkdir()
    offloader_dir = tmp_path / "offloader"
    offloader_dir.mkdir()

    receiver_bus = EventBus()
    offloader_bus = EventBus()
    receiver = make_remote_build_controller(config_dir=receiver_dir, bus=receiver_bus)
    offloader = make_remote_build_controller(config_dir=offloader_dir, bus=offloader_bus)
    # Subscribe to OPENED before the offloader's
    # ``PeerLinkClient`` task has had a chance to fire it. The
    # listener captures every event; ``wait_until_session_opened``
    # waits on the captured-list's ``received`` event.
    opened_events = capture_events(offloader_bus, EventType.OFFLOADER_PEER_LINK_OPENED)

    # Stand up the receiver's peer-link WS endpoint on a real
    # TCP port. ``TestServer`` picks an ephemeral port; the
    # offloader dials ``("127.0.0.1", server.port)``.
    app = web.Application()
    handler = await make_peer_link_handler(receiver, receiver_dir)
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    assert server.port is not None  # TestServer always binds; narrow for type-checkers.

    # Both controllers start before any pair-flow calls — the
    # offloader needs its pair-status listener slot wired so
    # ``request_pair`` can register the per-row long-poll task,
    # and the receiver needs its identity + handler factory ready
    # so the offloader's WS dials succeed.
    await receiver.start()
    await offloader.start()

    # 1. Receiver opens the pairing window so its handler will
    #    accept ``intent="pair_request"`` frames.
    await receiver.set_pairing_window(open=True, client="receiver-tab")

    # 2. Offloader runs preview to capture the receiver's pin
    #    over a live Noise XX handshake.
    preview = await offloader.preview_pair(hostname="127.0.0.1", port=server.port)
    pin_sha256 = preview["pin_sha256"]

    # 3. Offloader requests pairing. Receiver lands a PENDING
    #    ``StoredPeer`` and fires REMOTE_BUILD_PAIR_REQUEST_RECEIVED;
    #    the offloader spawns its pair-status long-poll listener
    #    against this row.
    await offloader.request_pair(
        hostname="127.0.0.1",
        port=server.port,
        pin_sha256=pin_sha256,
        receiver_label="receiver",
        offloader_label="offloader",
    )

    # 4. Receiver-side admin clicks Accept. The PENDING peer's
    #    ``dashboard_id`` is the offloader's stable identity —
    #    pull it off the row the receiver just landed. Subscribe
    #    to OFFLOADER_PAIR_STATUS_CHANGED *before* approve_peer
    #    fires so the receiver's APPROVED → offloader's
    #    pair-status listener → status-flip-event chain can be
    #    awaited deterministically rather than spun on.
    [pending_dashboard_id] = list(receiver._pending_peers.keys())
    pair_status_changed = capture_events(offloader_bus, EventType.OFFLOADER_PAIR_STATUS_CHANGED)
    await receiver.approve_peer(dashboard_id=pending_dashboard_id)

    # 5. Wait for the offloader's pair-status listener to observe
    #    the flip. The listener's long-poll WS unblocks on the
    #    receiver's bus event, then ``_apply_pair_status_result``
    #    flips the local row to APPROVED, fires
    #    OFFLOADER_PAIR_STATUS_CHANGED, and spawns the long-lived
    #    peer-link client.
    await asyncio.wait_for(pair_status_changed.received.wait(), timeout=2.0)
    assert pair_status_changed[-1]["status"] == "approved"

    instances = PairedInstances(
        receiver=receiver,
        offloader=offloader,
        receiver_server=server,
        receiver_bus=receiver_bus,
        offloader_bus=offloader_bus,
        offloader_dashboard_id=pending_dashboard_id,
        _opened=opened_events,
    )
    try:
        yield instances
    finally:
        # Teardown order matters: the offloader's ``stop()``
        # cancels its peer-link client task, whose
        # ``CancelledError`` handler sends a structured
        # ``terminate{client_stopped}`` frame to the receiver.
        # Stopping the receiver first would race that frame
        # against the receiver's WS shutdown.
        await offloader.stop()
        await receiver.stop()
        await server.close()
