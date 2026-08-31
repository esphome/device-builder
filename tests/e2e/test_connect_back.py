"""End-to-end: receiver connect-back rebinds the offloader and the session recovers."""

from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from esphome_device_builder.api.ws import init_ws_app
from esphome_device_builder.controllers.remote_build import connect_back as rb_connect_back
from esphome_device_builder.controllers.remote_build.peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.models import EventType

from ..conftest import capture_events
from .conftest import PairedInstances


async def _start_peer_link_server(
    instances: PairedInstances, *, connect_back_only: bool
) -> TestServer:
    """Bind a peer-link listener: the offloader's connect-back-only one, or a fresh receiver one."""
    handles = instances.offloader_handles if connect_back_only else instances.receiver_handles
    identity = await handles._db.peer_link_identity_store.async_load()
    handler = make_peer_link_handler(
        handles.receiver,
        identity,
        offloader=instances.offloader if connect_back_only else None,
        accept_receiver_intents=not connect_back_only,
    )
    app = web.Application()
    init_ws_app(app)
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_connect_back_rebinds_offloader_and_forward_session_recovers(
    paired_instances: PairedInstances,
) -> None:
    """Full loop: forward path dies, receiver dials back, offloader probes + rebinds, reconnects."""
    instances = paired_instances
    await instances.wait_until_session_opened()

    connect_back_server = await _start_peer_link_server(instances, connect_back_only=True)
    new_receiver_server = await _start_peer_link_server(instances, connect_back_only=False)
    rebound = capture_events(instances.offloader_bus, EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND)
    try:
        # The receiver moves: its old endpoint dies and the offloader's
        # forward client is left retrying stale coordinates.
        await instances.receiver_server.close()
        await instances.wait_until_session_closed()

        peer = instances.receiver.state.approved_peers[instances.offloader_dashboard_id]
        assert peer.peer_ip == "127.0.0.1"
        peer.connect_back_port = connect_back_server.port or 0

        assert new_receiver_server.port is not None
        await rb_connect_back._dial_peer(
            instances.receiver, peer, announce_port=new_receiver_server.port
        )

        pairing = instances.offloader.state.pairings[instances.pin_sha256]
        assert (pairing.receiver_hostname, pairing.receiver_port) == (
            "127.0.0.1",
            new_receiver_server.port,
        )
        await asyncio.wait_for(rebound.received.wait(), timeout=10.0)
        # The respawned forward client reconnects at the new endpoint.
        await instances.offloader_opened.wait_for_match(
            lambda event: event.get("receiver_port") == new_receiver_server.port,
            timeout=10.0,
            what="reconnect at new endpoint",
        )
        assert instances.pin_sha256 in instances.offloader.state.open_peer_links
    finally:
        await connect_back_server.close()
        await new_receiver_server.close()


async def test_connect_back_refused_while_forward_session_live(
    paired_instances: PairedInstances,
) -> None:
    """A live forward link wins: the announce is refused and nothing is persisted."""
    instances = paired_instances
    await instances.wait_until_session_opened()

    connect_back_server = await _start_peer_link_server(instances, connect_back_only=True)
    try:
        peer = instances.receiver.state.approved_peers[instances.offloader_dashboard_id]
        peer.peer_ip = "127.0.0.1"
        peer.connect_back_port = connect_back_server.port or 0

        await rb_connect_back._dial_peer(instances.receiver, peer, announce_port=6055)

        pairing = instances.offloader.state.pairings[instances.pin_sha256]
        assert pairing.receiver_port == instances.receiver_server.port
        cooldowns = instances.receiver.state.connect_back_cooldowns
        assert cooldowns.strikes(instances.offloader_dashboard_id) == 1
        assert not cooldowns.ready(instances.offloader_dashboard_id)
        assert instances.pin_sha256 in instances.offloader.state.open_peer_links
    finally:
        await connect_back_server.close()
