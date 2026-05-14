"""End-to-end: WS-client-driven local ``firmware/compile`` round-trip.

Pins the full path a real frontend walks for a local compile:

    aiohttp WS open → ``ServerInfoMessage`` → ``subscribe_events``
    (initial_state + ``{subscribed: True}``) → ``firmware/compile``
    (FirmwareJob ack) → live ``job_started`` / ``job_output`` /
    ``job_completed`` event frames

Existing coverage stops short of this combination:

* ``tests/controllers/firmware/test_execute_job_e2e.py`` drives the
  runner pipeline directly through ``controller.compile`` — no WS
  dispatch, no ``@api_command`` registration, no bus → WS event
  forwarding.
* ``tests/test_ws_handler_branches.py`` exercises the WS dispatch
  loop against a ``MagicMock`` device-builder — no real controller,
  no event bus.
* ``tests/e2e/test_install_round_trip.py`` covers the *remote*
  install round-trip (two paired dashboards, real Noise) — local
  install is a separate code path.

A regression that broke any of (``@api_command`` collection,
``command_handlers["firmware/compile"]`` registration,
``subscribe_events``'s bus→WS forwarding, the WS streaming-event
serialisation of ``JobLifecycleData``) would slip past every
single-concern test above but surface here.

The subprocess is the same ``sys.executable -c '<script>'``
substitution used in the controller-level test
(``_fake_esphome`` in ``test_execute_job_e2e.py``) — wall-clock
stays sub-second and we don't need a real esphome install.
"""

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
    """Drain WS frames until *predicate(frame)* is truthy; return that frame.

    Frames before the match are discarded; tests that want to
    inspect intermediate frames build their own loop. A hard
    deadline guards against a regression that drops the awaited
    event so the test fails cleanly rather than hanging pytest.
    """
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
    """Real ``DeviceBuilder`` wired into an aiohttp WS test client.

    Composes the existing hermetic fixtures: ``make_settings``
    provides the ``DashboardSettings`` rooted at ``tmp_path`` plus
    ``CORE.config_path``; ``_hermetic_lifecycle`` stubs the
    network / subprocess surfaces so ``db.start()`` runs without
    touching mDNS, MQTT, the disk catalogs, or a real ``esphome``
    install. The WS app is built directly via :func:`init_ws_app`
    + :func:`create_ws_routes` rather than ``db.create_app()`` to
    skip the frontend-serving wiring and the implicit lifecycle
    hook (we own the ``start`` / ``stop`` ordering here).
    """
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
    """``firmware/compile`` over the wire runs the subprocess and pushes ``job_completed``.

    Pins each leg the dashboard frontend depends on:

    1. The WS opens, ``ServerInfoMessage`` lands first.
    2. ``subscribe_events`` registers bus → WS forwarding; the
       initial-state seed arrives, followed by the
       ``{subscribed: True}`` ack.
    3. ``firmware/compile`` returns a ``FirmwareJob`` ack on the
       same WS.
    4. The runner spawns the (fake) subprocess; bus events fan
       out as streaming ``event`` frames keyed on the event-type
       string (``job_started`` / ``job_output`` / ``job_completed``).
    5. Server-side job state lands at ``COMPLETED`` with the
       captured output preserved.
    """
    db, client = local_dashboard
    assert db.firmware is not None
    db.firmware.state.esphome_cmd = [sys.executable, "-c", _FAKE_ESPHOME_OK]
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    async with client.ws_connect("/ws") as ws:
        info = (await ws.receive(timeout=2.0)).json()
        assert info["requires_auth"] is False

        await _send_command(ws, "subscribe_events", "sub-1")
        # The handler pushes initial_state first, then the result.
        # Drain through the result so subsequent reads land on
        # live bus events.
        await _recv_until(
            ws,
            predicate=lambda f: f.get("event") == "initial_state",
        )
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
        # The streaming event for ``JobLifecycleData`` serialises
        # the ``job`` field through ``FirmwareJob.to_dict``.
        assert completed["data"]["job"]["job_id"] == job_id
        assert completed["data"]["job"]["status"] == JobStatus.COMPLETED.value

    # Server-side state mirrors the wire verdict and the captured
    # output survived for late-attaching followers.
    job = db.firmware.state.jobs[job_id]
    assert job.status is JobStatus.COMPLETED
    assert job.exit_code == 0
    assert any("Compile finished" in line for line in job.output)
