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

The pair flow itself is short-circuited to keep the harness
focused: the ``paired_instances`` fixture seeds an APPROVED
``StoredPeer`` on the receiver and an APPROVED
``StoredPairing`` on the offloader before either controller
starts, mimicking the in-memory state the request → approve
flow would have left. The flow itself is covered by the
single-side tests; the harness picks up at "both sides know
about each other, now what."
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from esphome_device_builder.controllers.remote_build import RemoteBuildController
from esphome_device_builder.controllers.remote_build_peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.helpers.dashboard_identity import get_or_create_identity
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.peer_link_identity import (
    get_or_create_peer_link_identity,
)
from esphome_device_builder.helpers.peer_link_noise import pin_sha256_for_pubkey
from esphome_device_builder.models import (
    EventType,
    PeerStatus,
    StoredPairing,
    StoredPeer,
)


def _make_controller(*, config_dir: Path, bus: EventBus) -> RemoteBuildController:
    """Construct a :class:`RemoteBuildController` against a stub :class:`DeviceBuilder`.

    Mirrors the per-test ``_make_controller`` helpers in the
    single-side tests but wires a *real* :class:`EventBus` (not a
    :class:`MagicMock`) so events fire end-to-end through the
    fan-out fabric the production dispatcher uses.
    """
    db = MagicMock()
    db.devices = MagicMock()
    db.devices.zeroconf = None
    db._dashboard_advertiser = None
    db.settings = MagicMock()
    db.settings.config_dir = config_dir
    db.bus = bus
    return RemoteBuildController(db)


async def _wait_until(condition: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Yield to the loop until *condition()* returns truthy or *timeout* elapses.

    Raises :exc:`TimeoutError` (via :func:`asyncio.wait_for`) when
    the condition stays false past *timeout*. Mirrors the helper
    in the single-side test files; copied here so the e2e harness
    can stand alone without cross-importing test utilities.
    """

    async def _spin() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(_spin(), timeout=timeout)


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

    async def wait_until_session_opened(self, *, timeout: float = 2.0) -> None:
        """Block until the receiver has registered the offloader's peer-link session.

        Both sides observing the session is the natural "harness
        is fully primed" signal: the offloader's
        :class:`PeerLinkClient` has completed the Noise XX
        handshake, the receiver's ``_run_peer_link_session`` has
        registered the session in
        :attr:`RemoteBuildController._peer_link_sessions`, and
        the dispatch loop is parked on its receive iterator.
        Tests that exercise application-message behaviour
        (5b/5c/5d) start from this state.
        """
        await _wait_until(
            lambda: self.offloader_dashboard_id in self.receiver._peer_link_sessions,
            timeout=timeout,
        )


@pytest.fixture
async def paired_instances(
    tmp_path: Path,
) -> AsyncGenerator[PairedInstances, None]:
    """Yield two :class:`RemoteBuildController` instances pre-paired and connected.

    The receiver runs its peer-link Noise WS handler bound to a
    :class:`aiohttp.test_utils.TestServer` on a real ephemeral
    TCP port; the offloader runs a real
    :class:`PeerLinkClient` against it. Both sides have the
    other's APPROVED row pre-seeded in RAM so the offloader's
    ``start()`` immediately spawns the long-lived session
    without going through the full request → approve handshake
    (covered by the single-side tests).

    Per-side event buses are real, so production-shape event
    fan-out runs end-to-end. Teardown drains both controllers
    in dependency order: offloader first (its client task sends
    a ``terminate{client_stopped}`` to the receiver, the
    receiver's session loop unwinds), then the receiver (closing
    any remaining server-side state), then the TestServer.
    """
    receiver_dir = tmp_path / "receiver"
    receiver_dir.mkdir()
    offloader_dir = tmp_path / "offloader"
    offloader_dir.mkdir()

    receiver_bus = EventBus()
    offloader_bus = EventBus()
    receiver = _make_controller(config_dir=receiver_dir, bus=receiver_bus)
    offloader = _make_controller(config_dir=offloader_dir, bus=offloader_bus)

    # Pre-create both identities on disk so we can wire the
    # cross-references (receiver-side ``StoredPeer`` carrying
    # the offloader's pubkey, offloader-side ``StoredPairing``
    # carrying the receiver's pubkey) before either ``start()``
    # runs and lazily generates them.
    loop = asyncio.get_running_loop()
    receiver_identity = await loop.run_in_executor(
        None, get_or_create_peer_link_identity, receiver_dir
    )
    offloader_identity = await loop.run_in_executor(
        None, get_or_create_peer_link_identity, offloader_dir
    )
    offloader_dashboard = await loop.run_in_executor(None, get_or_create_identity, offloader_dir)

    # Stand up the receiver's peer-link WS endpoint on a real
    # TCP port. ``TestServer`` picks an ephemeral port; the
    # offloader dials ``("127.0.0.1", server.port)``.
    app = web.Application()
    handler = await make_peer_link_handler(receiver, receiver_dir)
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    assert server.port is not None  # TestServer always binds; narrow for type-checkers.

    paired_at = time.time()
    receiver._approved_peers[offloader_dashboard.dashboard_id] = StoredPeer(
        dashboard_id=offloader_dashboard.dashboard_id,
        pin_sha256=pin_sha256_for_pubkey(offloader_identity.public_bytes),
        static_x25519_pub=offloader_identity.public_bytes,
        label=offloader_dashboard.dashboard_id,
        paired_at=paired_at,
    )
    offloader._pairings[("127.0.0.1", server.port)] = StoredPairing(
        receiver_hostname="127.0.0.1",
        receiver_port=server.port,
        pin_sha256=pin_sha256_for_pubkey(receiver_identity.public_bytes),
        static_x25519_pub=receiver_identity.public_bytes,
        label="receiver",
        paired_at=paired_at,
        status=PeerStatus.APPROVED,
    )

    # ``start()``'s on-disk load is a no-op for a fresh tmp dir,
    # so the pre-seeded RAM dicts above survive. The offloader's
    # ``start()`` spawns the long-lived peer-link client for
    # every APPROVED ``StoredPairing`` (just the one we seeded).
    await receiver.start()
    await offloader.start()

    instances = PairedInstances(
        receiver=receiver,
        offloader=offloader,
        receiver_server=server,
        receiver_bus=receiver_bus,
        offloader_bus=offloader_bus,
        offloader_dashboard_id=offloader_dashboard.dashboard_id,
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


def capture_events(bus: EventBus, event_type: EventType) -> _CapturedEvents:
    """Subscribe to *event_type* on *bus* and return a list captured-as-they-fire.

    Mirror of the helper in
    ``test_remote_build_peer_link_client.py``; copied here so the
    harness module is self-contained. Returned list is a thin
    subclass that exposes a ``received`` :class:`asyncio.Event`
    set on each append, so tests block via
    ``await asyncio.wait_for(captured.received.wait(), timeout=...)``
    rather than polling.
    """
    captured = _CapturedEvents()
    bus.add_listener(event_type, lambda event: captured.append(dict(event.data)))
    return captured


class _CapturedEvents(list[dict]):
    """A list of captured event payloads with an :class:`asyncio.Event` set on each append."""

    def __init__(self) -> None:
        super().__init__()
        self.received = asyncio.Event()

    def append(self, item: dict) -> None:
        super().append(item)
        self.received.set()
