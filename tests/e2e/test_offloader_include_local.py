"""WS-client-driven round-trip for the ``include_local_in_pool`` advanced toggle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.device_builder import DeviceBuilder

from ..conftest import MakeSettingsFactory


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
) -> Any:
    """Real ``DeviceBuilder`` (offloader up) wired into an aiohttp WS test client."""
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


async def _subscribe_and_get_initial(ws: Any, message_id: str) -> dict[str, Any]:
    """Subscribe and return the ``initial_state`` snapshot payload."""
    await _send_command(ws, "subscribe_events", message_id)
    initial = await _recv_until(ws, predicate=lambda f: f.get("event") == "initial_state")
    return initial["data"]


async def test_include_local_toggle_round_trip_over_ws(
    local_dashboard: tuple[DeviceBuilder, Any],
) -> None:
    """``set_offloader_settings(include_local_in_pool=True)`` seeds initial_state and fans an event.

    Proves the full wire contract end to end: the snapshot
    default, the command ack view, the cross-tab event, and a
    fresh subscriber seeing the persisted value on first paint.
    """
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
