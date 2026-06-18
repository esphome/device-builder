"""WS-client-driven round-trip for the ``include_local_in_pool`` advanced toggle."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.controllers.firmware import remote_dispatch
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.models import JobSource, JobStatus
from esphome_device_builder.models.remote_build import (
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    StoredPairing,
)

from ..conftest import MakeSettingsFactory

# Local compile stub: prints the two lines the runner scrapes and exits 0, so a
# LOCAL-routed compile reaches JOB_COMPLETED without a real toolchain.
_FAKE_ESPHOME_OK = (
    "import sys\n"
    "print('INFO Reading configuration kitchen.yaml...')\n"
    "print('INFO Compile finished.')\n"
    "sys.exit(0)\n"
)

_PIN = "a" * 64


async def _send_command(ws: Any, command: str, message_id: str, **args: Any) -> None:
    """Send a ``CommandMessage``-shaped frame over *ws*."""
    await ws.send_json({"command": command, "message_id": message_id, "args": args})


async def _recv_until(ws: Any, *, predicate: Any, timeout: float = 10.0) -> dict[str, Any]:
    """Drain WS frames until *predicate(frame)* is truthy; return that frame."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            msg = "timed out waiting for predicate to match"
            raise TimeoutError(msg)
        frame = (await ws.receive(timeout=remaining)).json()
        if predicate(frame):
            return frame


@pytest.fixture
async def local_dashboard(
    make_settings: MakeSettingsFactory,
    _hermetic_lifecycle: None,
    aiohttp_client: AiohttpClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Real ``DeviceBuilder`` (offloader up) wired into an aiohttp WS test client."""
    settings = make_settings(with_core_path=True)
    settings.using_password = False
    # Skip the 20s dispatch-loop startup grace so the first matcher pass runs at once.
    monkeypatch.setattr(remote_dispatch, "_STARTUP_GRACE_SECONDS", 0)
    db = DeviceBuilder(settings)
    await db.start()

    app = web.Application()
    app["device_builder"] = db
    app["trusted_site"] = True
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)
    try:
        yield db, client
    finally:
        await db.stop()


async def _subscribe_and_get_initial(ws: Any, message_id: str) -> dict[str, Any]:
    """Subscribe and return the ``initial_state`` snapshot payload."""
    await _send_command(ws, "subscribe_events", message_id)
    initial = await _recv_until(ws, predicate=lambda f: f.get("event") == "initial_state")
    return initial["data"]


async def test_include_local_toggle_round_trip_over_ws(
    local_dashboard: tuple[DeviceBuilder, Any],
) -> None:
    """Pins the WS wire contract: snapshot default, command ack, cross-tab event, reseed."""
    db, client = local_dashboard
    assert db.remote_build_offloader is not None

    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)  # server_version / requires_auth handshake

        initial = await _subscribe_and_get_initial(ws, "sub-1")
        assert initial["include_local_in_pool"] is False

        await _send_command(
            ws, "remote_build/set_offloader_settings", "set-1", include_local_in_pool=True
        )
        # The command ``result`` and the cross-tab stream event race; collect
        # both regardless of arrival order.
        event: dict[str, Any] | None = None
        ack: dict[str, Any] | None = None
        while event is None or ack is None:
            frame = await _recv_until(
                ws,
                predicate=lambda f: (
                    f.get("event") == "offloader_include_local_changed"
                    or (f.get("message_id") == "set-1" and "result" in f)
                ),
            )
            if frame.get("event") == "offloader_include_local_changed":
                event = frame
            else:
                ack = frame
        assert event["data"] == {"include_local_in_pool": True}
        assert ack["result"]["include_local_in_pool"] is True

    # In-RAM state flipped, so a fresh subscriber paints the new value immediately.
    assert db.remote_build_offloader.state.include_local_in_pool is True
    async with client.ws_connect("/ws") as ws2:
        await ws2.receive(timeout=2.0)
        initial2 = await _subscribe_and_get_initial(ws2, "sub-2")
        assert initial2["include_local_in_pool"] is True


def _seed_busy_build_server(db: DeviceBuilder) -> None:
    """Make the offloader report one eligible-but-busy paired build server.

    Eligible + idle at submit (so the compile is held ``REMOTE_PENDING``), then
    pinned busy at dispatch via a fake in-flight pool entry — so the dispatcher
    must choose between WAIT and the local lane without a real receiver in the
    loop. The fake entry never clears, modelling a server busy with another build.
    """
    offloader = db.remote_build_offloader
    assert offloader is not None
    offloader.state.pairings[_PIN] = StoredPairing(
        receiver_hostname="build.local",
        receiver_port=6055,
        pin_sha256=_PIN,
        static_x25519_pub=b"\x00" * 32,
        label="desktop",
        paired_at=1.0,
        status=PeerStatus.APPROVED,
        enabled=True,
        esphome_version="",
    )
    offloader.state.open_peer_links.add(_PIN)
    offloader.state.peer_queue_status[_PIN] = PeerQueueStatusSnapshotEntry(
        receiver_hostname="build.local",
        receiver_port=6055,
        pin_sha256=_PIN,
        idle=True,
        running=False,
        queue_depth=0,
    )
    db.firmware.state.remote_dispatch.job_peer["busy-other"] = _PIN


async def test_include_local_runs_overflow_compile_on_local_lane(
    local_dashboard: tuple[DeviceBuilder, Any],
    tmp_path: Path,
) -> None:
    """Opt-in on + the only server busy → the compile runs on the local lane and completes.

    Drives the real WS compile path through the real queue + dispatch loop: the
    job is held ``REMOTE_PENDING`` at submit, then the dispatcher routes it LOCAL
    because the lone server is busy and ``include_local_in_pool`` is on.
    """
    db, client = local_dashboard
    db.firmware.state.esphome_cmd = [sys.executable, "-c", _FAKE_ESPHOME_OK]
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)  # server_version / requires_auth handshake
        await _subscribe_and_get_initial(ws, "sub-1")
        await _send_command(
            ws, "remote_build/set_offloader_settings", "set-1", include_local_in_pool=True
        )
        await _recv_until(ws, predicate=lambda f: f.get("message_id") == "set-1" and "result" in f)

        _seed_busy_build_server(db)

        await _send_command(ws, "firmware/compile", "comp-1", configuration="kitchen.yaml")
        ack = await _recv_until(
            ws, predicate=lambda f: f.get("message_id") == "comp-1" and "result" in f
        )
        job_id = ack["result"]["job_id"]

        completed = await _recv_until(
            ws, predicate=lambda f: f.get("event") == "job_completed", timeout=15.0
        )
        assert completed["data"]["job"]["job_id"] == job_id

    job = db.firmware.state.jobs[job_id]
    assert job.status is JobStatus.COMPLETED
    # The busy server forced the overflow compile onto the local lane.
    assert job.source is JobSource.LOCAL
    assert job_id not in db.firmware.state.remote_dispatch.pending


async def test_include_local_off_overflow_compile_waits_for_server(
    local_dashboard: tuple[DeviceBuilder, Any],
    tmp_path: Path,
) -> None:
    """Opt-in off (default) → the compile holds for the busy server, never runs local."""
    db, client = local_dashboard
    db.firmware.state.esphome_cmd = [sys.executable, "-c", _FAKE_ESPHOME_OK]
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)
        initial = await _subscribe_and_get_initial(ws, "sub-1")
        assert initial["include_local_in_pool"] is False  # default off

        _seed_busy_build_server(db)

        await _send_command(ws, "firmware/compile", "comp-1", configuration="kitchen.yaml")
        ack = await _recv_until(
            ws, predicate=lambda f: f.get("message_id") == "comp-1" and "result" in f
        )
        job_id = ack["result"]["job_id"]

        # No local fallback: the compile holds in the pool for a free server, so
        # no terminal event arrives.
        with pytest.raises(TimeoutError):
            await _recv_until(
                ws, predicate=lambda f: f.get("event") == "job_completed", timeout=0.5
            )

    assert job_id in db.firmware.state.remote_dispatch.pending
    assert db.firmware.state.jobs[job_id].status is JobStatus.QUEUED
