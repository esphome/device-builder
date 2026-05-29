"""WS-client-driven local ``firmware/compile`` round-trip."""

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
from esphome_device_builder.models import JobStatus

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


async def _recv_until(
    ws: Any,
    *,
    predicate: Any,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Drain WS frames until *predicate(frame)* is truthy; return that frame."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            msg = "timed out waiting for predicate to match"
            raise TimeoutError(msg)
        msg_obj = await ws.receive(timeout=remaining)
        frame = msg_obj.json()
        if predicate(frame):
            return frame


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


async def test_local_compile_round_trip_over_ws(
    local_dashboard: tuple[DeviceBuilder, Any],
    tmp_path: Path,
) -> None:
    """``firmware/compile`` over the wire fans bus events back as streaming WS frames."""
    db, client = local_dashboard
    assert db.firmware is not None
    db.firmware.state.esphome_cmd = [sys.executable, "-c", _FAKE_ESPHOME_OK]
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    async with client.ws_connect("/ws") as ws:
        info = (await ws.receive(timeout=2.0)).json()
        assert info["requires_auth"] is False

        await _send_command(ws, "subscribe_events", "sub-1")
        await _recv_until(ws, predicate=lambda f: f.get("event") == "initial_state")
        ack = await _recv_until(
            ws,
            predicate=lambda f: f.get("message_id") == "sub-1" and "result" in f,
        )
        assert ack["result"] == {"subscribed": True}

        await _send_command(ws, "firmware/compile", "comp-1", configuration="kitchen.yaml")
        compile_ack = await _recv_until(
            ws,
            predicate=lambda f: f.get("message_id") == "comp-1" and "result" in f,
        )
        job_id = compile_ack["result"]["job_id"]
        assert compile_ack["result"]["configuration"] == "kitchen.yaml"

        completed = await _recv_until(
            ws,
            predicate=lambda f: f.get("event") == "job_completed",
            timeout=15.0,
        )
        assert completed["data"]["job"]["job_id"] == job_id
        assert completed["data"]["job"]["status"] == JobStatus.COMPLETED.value

        # Read the finished job's log back over the wire the way the
        # dashboard does: follow_job replays the stored output then
        # ends. Deterministic regardless of whether the post-completion
        # flush to the sidecar has landed yet (it replays RAM until the
        # flush clears it, the sidecar after).
        await _send_command(ws, "firmware/follow_job", "follow-1", job_id=job_id)
        output_lines: list[str] = []
        while True:
            frame = await _recv_until(ws, predicate=lambda f: f.get("message_id") == "follow-1")
            if frame.get("event") == "output":
                output_lines.append(frame["data"])
            elif frame.get("event") == "result":
                assert frame["data"]["status"] == JobStatus.COMPLETED.value
                break

    job = db.firmware.state.jobs[job_id]
    assert job.status is JobStatus.COMPLETED
    assert job.exit_code == 0
    assert any("Compile finished" in line for line in output_lines)
