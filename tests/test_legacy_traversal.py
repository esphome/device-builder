"""Endpoint-level coverage for legacy traversal rejection.

The legacy spawn endpoints (``GET /json-config``, ``GET /compile``,
``GET /upload``) call ``settings.rel_path`` directly rather than
flowing through the ``api_command`` dispatcher, so they need their
own ``CommandError`` handling. These tests pin the wire contracts
HA's ``esphome-dashboard-api`` expects on rejection: 403 for the
JSON endpoint, ``{event: "exit", code: 1}`` for the spawn handlers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api.legacy import create_legacy_routes
from esphome_device_builder.controllers.config import DashboardSettings


def _make_app(tmp_path: Path) -> web.Application:
    """Wire just enough DeviceBuilder shape for the legacy routes."""
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()

    app = web.Application()
    app["device_builder"] = type("DB", (), {"settings": settings})()
    app.add_routes(create_legacy_routes())
    return app


@pytest.mark.parametrize(
    "payload",
    ["../etc/passwd", "../../etc/passwd", "/absolute/path"],
)
async def test_json_config_rejects_traversal(
    tmp_path: Path, aiohttp_client: AiohttpClient, payload: str
) -> None:
    """``GET /json-config`` returns 403 on traversal-shaped configuration.

    Pre-#107 the rel_path raised ``ValueError``; this PR moved it
    to ``CommandError``. The endpoint's ``except`` clause has to
    follow or every traversal payload would 500.
    """
    client = await aiohttp_client(_make_app(tmp_path))
    resp = await client.get("/json-config", params={"configuration": payload})
    assert resp.status == 403
    body = await resp.json()
    assert body == {"error": "Forbidden"}


async def test_compile_ws_emits_exit_frame_on_traversal(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Legacy ``/compile`` WS sends a controlled exit frame on rejection.

    HA's ``esphome-dashboard-api`` only knows about ``{event: line}``
    and ``{event: exit, code}`` frames — letting ``CommandError``
    bubble would tear the WS down and surface as a connection drop
    on the client side. The exit-frame branch keeps the protocol
    contract so HA can show "compile rejected" in its log instead
    of "lost connection".
    """
    client = await aiohttp_client(_make_app(tmp_path))
    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "../etc/passwd"})
        msg = await ws.receive_json()
        assert msg == {"event": "exit", "code": 1}


async def test_upload_ws_emits_exit_frame_on_traversal(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Same contract as ``/compile`` — the spawn handler is shared."""
    client = await aiohttp_client(_make_app(tmp_path))
    async with client.ws_connect("/upload") as ws:
        await ws.send_json({"type": "spawn", "configuration": "../etc/passwd"})
        msg = await ws.receive_json()
        assert msg == {"event": "exit", "code": 1}
