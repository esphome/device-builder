"""
Shared helper for the manual remote-firmware e2e scripts.

Stands up two ``DeviceBuilder`` instances in one process, pairs them via the
production pairing flow over a real Noise XX peer-link WS, and yields handles
to both. The receiver side runs a real :class:`FirmwareController` so
``firmware/install`` / ``firmware/compile`` submitted on the offloader
trigger a real ``esphome compile`` on the receiver side; the offloader's
runner then materialises the receiver's artifacts back locally exactly like
production.

These scripts are *not* part of CI. They exist for wet-test confidence
before each release. Run them manually against a real device.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
from esphome.core import CORE

from esphome_device_builder.api.ws import init_ws_app
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.remote_build.peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.peer_link_identity import PeerLinkIdentityStore
from esphome_device_builder.models import EventType


@dataclass
class MockPair:
    """Handles for the offloader + receiver dashboards + pairing metadata."""

    offloader: DeviceBuilder
    receiver: DeviceBuilder
    receiver_server: TestServer
    pin_sha256: str
    offloader_dashboard_id: str


def _make_settings(config_dir: Path) -> DashboardSettings:
    """Build a minimal ``DashboardSettings`` rooted at *config_dir*.

    Pins the dashboard ports to 0 (OS-assigned) so two paired mock
    instances can coexist on a host that already runs a real
    device-builder dashboard.
    """
    settings = DashboardSettings()
    settings.config_dir = config_dir
    settings.absolute_config_dir = config_dir.resolve()
    settings.port = 0
    settings.remote_build_port = 0
    return settings


@asynccontextmanager
async def paired_dashboards(
    *,
    root_dir: Path,
    yaml_source: Path | None = None,
) -> AsyncIterator[MockPair]:
    """Yield two paired ``DeviceBuilder`` instances under *root_dir*.

    If *yaml_source* is given, copy it into the offloader's config dir
    so ``firmware/install`` / ``firmware/compile`` calls have a YAML
    to resolve. The receiver loads its own copy through the bundle the
    offloader ships.

    ``CORE.config_path`` is repointed at each side's sentinel during the
    relevant phase; tests of the materialiser then resolve under that
    side's data dir. The offloader's setup is what's active when the
    context manager yields.
    """
    offloader_dir = root_dir / "offloader"
    receiver_dir = root_dir / "receiver"
    offloader_dir.mkdir(parents=True, exist_ok=True)
    receiver_dir.mkdir(parents=True, exist_ok=True)
    # ``/var/folders/...`` resolves to ``/private/var/folders/...`` on
    # macOS; the receiver's ``relative_to`` against the extracted
    # YAML path needs both sides to match, so canonicalise here.
    offloader_dir = offloader_dir.resolve()
    receiver_dir = receiver_dir.resolve()

    if yaml_source is not None:
        target = offloader_dir / yaml_source.name
        target.write_bytes(yaml_source.read_bytes())
        # Pull in ``secrets.yaml`` from the same directory so
        # ``!secret`` references in the staged YAML resolve. Real
        # device YAMLs almost always lean on this; without it the
        # offloader's bundle step fails at config-read with
        # "No such file: secrets.yaml" before ever talking to the
        # receiver.
        companion_secrets = yaml_source.parent / "secrets.yaml"
        if companion_secrets.is_file():
            (offloader_dir / "secrets.yaml").write_bytes(companion_secrets.read_bytes())

    offloader_settings = _make_settings(offloader_dir)
    receiver_settings = _make_settings(receiver_dir)
    offloader = DeviceBuilder(offloader_settings)
    receiver = DeviceBuilder(receiver_settings)

    # Pin CORE.config_path to the offloader sentinel — the
    # offloader's materialise / firmware/download paths anchor
    # on this. (The receiver-side compile subprocess sets
    # ESPHOME_DATA_DIR explicitly so it doesn't depend on CORE.)
    offloader_sentinel = offloader_dir / "___DASHBOARD_SENTINEL___.yaml"
    CORE.config_path = offloader_sentinel

    await receiver.start()
    await offloader.start()
    assert receiver.remote_build_receiver is not None
    assert offloader.remote_build_offloader is not None

    server = await _start_receiver_peer_link_server(receiver, receiver_dir)
    pin_sha256, pending_dashboard_id = await _run_pair_flow(
        offloader=offloader, receiver=receiver, server=server
    )

    pair = MockPair(
        offloader=offloader,
        receiver=receiver,
        receiver_server=server,
        pin_sha256=pin_sha256,
        offloader_dashboard_id=pending_dashboard_id,
    )
    try:
        yield pair
    finally:
        # Offloader first so its peer-link client sends a
        # structured terminate before the receiver's WS unwinds.
        await offloader.stop()
        await receiver.stop()
        await server.close()


async def _start_receiver_peer_link_server(
    receiver: DeviceBuilder, receiver_dir: Path
) -> TestServer:
    """Stand up the receiver's peer-link WS on a real loopback port."""
    app = web.Application()
    init_ws_app(app)
    assert receiver.remote_build_receiver is not None
    identity = await PeerLinkIdentityStore(receiver_dir).async_load()
    handler = make_peer_link_handler(receiver.remote_build_receiver, identity)
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    assert server.port is not None
    return server


async def _run_pair_flow(
    *,
    offloader: DeviceBuilder,
    receiver: DeviceBuilder,
    server: TestServer,
) -> tuple[str, str]:
    """Run the production pair handshake; return ``(pin_sha256, dashboard_id)``.

    Also waits for the receiver's queue_status push so the
    scheduler treats the pairing as REMOTE-eligible by the time
    the caller submits a job.
    """
    assert receiver.remote_build_receiver is not None
    assert offloader.remote_build_offloader is not None
    assert server.port is not None

    await receiver.remote_build_receiver.set_pairing_window(open=True, client="manual-script")
    preview = await offloader.remote_build_offloader.preview_pair(
        hostname="127.0.0.1", port=server.port
    )
    pin_sha256 = preview["pin_sha256"]
    await offloader.remote_build_offloader.request_pair(
        hostname="127.0.0.1",
        port=server.port,
        pin_sha256=pin_sha256,
        receiver_label="receiver",
        offloader_label="offloader",
    )
    [pending_dashboard_id] = list(receiver.remote_build_receiver.state.pending_peers.keys())

    pair_event = asyncio.Event()
    queue_status_event = asyncio.Event()
    offloader.bus.add_listener(EventType.OFFLOADER_PAIR_STATUS_CHANGED, lambda _e: pair_event.set())
    offloader.bus.add_listener(
        EventType.OFFLOADER_QUEUE_STATUS_CHANGED, lambda _e: queue_status_event.set()
    )

    await receiver.remote_build_receiver.approve_peer(dashboard_id=pending_dashboard_id)
    await asyncio.wait_for(pair_event.wait(), timeout=5.0)
    # Scheduler needs the receiver's queue_status push before
    # firmware.compile / install routes REMOTE.
    await asyncio.wait_for(queue_status_event.wait(), timeout=5.0)
    return pin_sha256, pending_dashboard_id


async def wait_for_job(
    offloader: DeviceBuilder,
    job_id: str,
    *,
    timeout: float = 600.0,
) -> object:
    """Wait until *job_id* lands a terminal state on the offloader bus."""
    done = asyncio.Event()
    terminal_event: list[object] = []

    def _on_terminal(event: object) -> None:
        data = getattr(event, "data", None)
        if data is None:
            return
        job = data.get("job") if isinstance(data, dict) else None
        if job is None or job.job_id != job_id:
            return
        terminal_event.append(data)
        done.set()

    for ev_type in (EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED):
        offloader.bus.add_listener(ev_type, _on_terminal)

    await asyncio.wait_for(done.wait(), timeout=timeout)
    return terminal_event[0]
