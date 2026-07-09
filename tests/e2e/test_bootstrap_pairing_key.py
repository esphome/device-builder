"""
End-to-end: ``--remote-build-only`` bootstrap pairing over the real wire.

Two live controllers — a receiver driven by the production
``_bootstrap_first_pair`` orchestration and an offloader issuing
the real ``preview_pair`` / ``request_pair`` WS commands over
Noise XX. Pins the pairing-key gate end-to-end: a wrong key is
refused as a closed window without disarming, the banner-printed
key pairs (loosely retyped), and the pairing carries through to
an open peer-link session.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from esphome_device_builder import _remote_build_only as rbo
from esphome_device_builder.api.ws import init_ws_app
from esphome_device_builder.controllers.remote_build.peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.peer_link_identity import PeerLinkIdentityStore
from esphome_device_builder.models import ErrorCode, EventType, PeerStatus

from ..conftest import (
    RemoteBuildTestHandles,
    _CapturedEvents,
    capture_events,
    make_remote_build_controller,
)


@dataclass
class BootstrapInstances:
    """A bootstrap-armed receiver + an unpaired offloader on a live TestServer."""

    receiver_handles: RemoteBuildTestHandles
    offloader_handles: RemoteBuildTestHandles
    server: TestServer
    offloader_opened: _CapturedEvents
    receiver_opened: _CapturedEvents


@asynccontextmanager
async def _bootstrap_instances_ctx(tmp_path: Path) -> AsyncIterator[BootstrapInstances]:
    receiver_dir = tmp_path / "receiver"
    receiver_dir.mkdir()
    offloader_dir = tmp_path / "offloader"
    offloader_dir.mkdir()
    receiver_bus = EventBus()
    offloader_bus = EventBus()
    receiver = make_remote_build_controller(config_dir=receiver_dir, bus=receiver_bus)
    # Simulate the headless key-mode server so preview reports requires_pairing_key.
    receiver.receiver._db.settings.remote_build_only = True
    offloader = make_remote_build_controller(config_dir=offloader_dir, bus=offloader_bus)
    offloader_opened = capture_events(offloader_bus, EventType.OFFLOADER_PEER_LINK_OPENED)
    receiver_opened = capture_events(receiver_bus, EventType.RECEIVER_PEER_LINK_SESSION_OPENED)

    app = web.Application()
    init_ws_app(app)
    handler = make_peer_link_handler(
        receiver.receiver, await PeerLinkIdentityStore(receiver_dir).async_load()
    )
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    await receiver.start()
    await offloader.start()
    try:
        yield BootstrapInstances(
            receiver_handles=receiver,
            offloader_handles=offloader,
            server=server,
            offloader_opened=offloader_opened,
            receiver_opened=receiver_opened,
        )
    finally:
        await offloader.stop()
        await receiver.stop()
        await server.close()


async def test_bootstrap_pairing_key_round_trip(tmp_path: Path) -> None:
    """Wrong key refused without disarming; the banner key pairs and opens a session."""
    async with _bootstrap_instances_ctx(tmp_path) as inst:
        receiver = inst.receiver_handles.receiver
        offloader = inst.offloader_handles.offloader
        bootstrap = asyncio.create_task(
            rbo._bootstrap_first_pair(receiver._db, receiver)  # type: ignore[arg-type]
        )
        while not receiver.is_pairing_window_open():
            await asyncio.sleep(0.01)
        key = receiver.state.bootstrap_pairing_key
        assert key is not None

        preview = await offloader.preview_pair(hostname="127.0.0.1", port=inst.server.port)
        pin_sha256 = preview["pin_sha256"]
        # The armed bootstrap receiver reports it needs a key, so the UI can
        # require it up front instead of failing the first attempt.
        assert preview["requires_pairing_key"] is True

        # Wrong key: refused as a closed window, nothing persisted, still armed.
        with pytest.raises(CommandError) as excinfo:
            await offloader.request_pair(
                hostname="127.0.0.1",
                port=inst.server.port,
                pin_sha256=pin_sha256,
                receiver_label="build server",
                offloader_label="main builder",
                pairing_key="WRNG-WRNG-WRNG-WRNG",
            )
        assert excinfo.value.code is ErrorCode.NO_PAIRING_WINDOW
        assert receiver.state.approved_peers == {}
        assert receiver.state.auto_approve_first_pair
        assert not bootstrap.done()
        assert offloader.state.pairings == {}

        # The banner key, loosely retyped, pairs in one round-trip.
        summary = await offloader.request_pair(
            hostname="127.0.0.1",
            port=inst.server.port,
            pin_sha256=pin_sha256,
            receiver_label="build server",
            offloader_label="main builder",
            pairing_key=key.lower().replace("-", " "),
        )
        assert summary.status is PeerStatus.APPROVED
        assert summary.pin_sha256 == pin_sha256
        assert await asyncio.wait_for(bootstrap, timeout=2.0) is True

        # Receiver landed the approved row, closed the window, cleared the key.
        [peer] = receiver.state.approved_peers.values()
        assert peer.label == "main builder"
        assert not receiver.is_pairing_window_open()
        assert not receiver.state.auto_approve_first_pair
        assert receiver.state.bootstrap_pairing_key is None

        # The pairing carries through to a live peer-link session on both sides.
        await asyncio.wait_for(inst.offloader_opened.received.wait(), timeout=2.0)
        await asyncio.wait_for(inst.receiver_opened.received.wait(), timeout=2.0)
        assert peer.dashboard_id in receiver.state.peer_link_sessions


async def test_bootstrap_pair_without_key_refused(tmp_path: Path) -> None:
    """An offloader that sends no key (pre-key builder) cannot win the window."""
    async with _bootstrap_instances_ctx(tmp_path) as inst:
        receiver = inst.receiver_handles.receiver
        offloader = inst.offloader_handles.offloader
        bootstrap = asyncio.create_task(
            rbo._bootstrap_first_pair(receiver._db, receiver)  # type: ignore[arg-type]
        )
        while not receiver.is_pairing_window_open():
            await asyncio.sleep(0.01)

        preview = await offloader.preview_pair(hostname="127.0.0.1", port=inst.server.port)
        with pytest.raises(CommandError) as excinfo:
            await offloader.request_pair(
                hostname="127.0.0.1",
                port=inst.server.port,
                pin_sha256=preview["pin_sha256"],
                receiver_label="build server",
                offloader_label="main builder",
            )
        assert excinfo.value.code is ErrorCode.NO_PAIRING_WINDOW
        assert receiver.state.approved_peers == {}
        assert receiver.state.auto_approve_first_pair

        bootstrap.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bootstrap
