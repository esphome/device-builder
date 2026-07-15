"""WS-client-driven check that ``initial_state`` seeds a late tab's first paint."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.models import DEFAULT_CLEANUP_TTL_SECONDS, JobStatus

from ..conftest import MakeSettingsFactory

_FAKE_ESPHOME_OK = (
    "import sys\n"
    "print('INFO Reading configuration kitchen.yaml...')\n"
    "print('INFO Compile finished.')\n"
    "sys.exit(0)\n"
)


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


async def _subscribe_and_get_initial(ws: Any, message_id: str) -> dict[str, Any]:
    """Subscribe and return the ``initial_state`` snapshot payload."""
    await _send_command(ws, "subscribe_events", message_id)
    initial = await _recv_until(ws, predicate=lambda f: f.get("event") == "initial_state")
    return initial["data"]


@pytest.fixture
async def local_dashboard(
    make_settings: MakeSettingsFactory,
    _hermetic_lifecycle: None,
    aiohttp_client: AiohttpClient,
    tmp_path: Path,
) -> Any:
    """Real ``DeviceBuilder`` wired into an aiohttp WS test client."""
    settings = make_settings(with_core_path=True)
    settings.using_password = False
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


async def test_initial_state_seeds_receiver_settings_and_jobs(
    local_dashboard: tuple[DeviceBuilder, Any],
    tmp_path: Path,
) -> None:
    """A late tab paints the Build server panel from the snapshot alone.

    First connection: defaults in the snapshot, then a settings write
    and a full local compile. Second connection: the snapshot already
    carries the written settings and the finished job (without its
    ``output`` buffer) before any follow_jobs subscription exists.
    """
    db, client = local_dashboard
    assert db.firmware is not None
    db.firmware.state.esphome_cmd = [sys.executable, "-c", _FAKE_ESPHOME_OK]
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)  # server_version handshake

        initial = await _subscribe_and_get_initial(ws, "sub-1")
        assert initial["remote_build_settings"] == {
            "enabled": True,
            "cleanup_ttl_seconds": DEFAULT_CLEANUP_TTL_SECONDS,
        }
        assert initial["firmware_jobs"] == []

        await _send_command(
            ws, "remote_build/set_settings", "set-1", enabled=True, cleanup_ttl_seconds=7200
        )
        await _recv_until(ws, predicate=lambda f: f.get("message_id") == "set-1" and "result" in f)

        await _send_command(ws, "firmware/compile", "comp-1", configuration="kitchen.yaml")
        completed = await _recv_until(
            ws, predicate=lambda f: f.get("event") == "job_completed", timeout=15.0
        )
        job_id = completed["data"]["job"]["job_id"]

    # A second tab connecting later sees everything in the snapshot.
    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)

        initial = await _subscribe_and_get_initial(ws, "sub-2")
        assert initial["remote_build_settings"] == {
            "enabled": True,
            "cleanup_ttl_seconds": 7200,
        }
        jobs = {job["job_id"]: job for job in initial["firmware_jobs"]}
        assert jobs[job_id]["status"] == JobStatus.COMPLETED.value
        assert jobs[job_id]["configuration"] == "kitchen.yaml"
        # Same projection follow_jobs replays: no live output buffer.
        assert "output" not in jobs[job_id]
