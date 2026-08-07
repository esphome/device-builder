"""Wire contract of the per-connection ServerInfoMessage handshake."""

from __future__ import annotations

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.api.ws import create_ws_routes, init_ws_app
from esphome_device_builder.helpers.json import loads

from .conftest import make_ws_device_builder


def _bare_app(*, trusted: bool = True, **kwargs: str | bool) -> web.Application:
    device_builder = make_ws_device_builder(**kwargs)
    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = trusted
    init_ws_app(app)
    app.router.add_routes(create_ws_routes())
    return app


@pytest.mark.parametrize("in_docker", [True, False])
async def test_server_info_forwards_in_docker(
    aiohttp_client: AiohttpClient,
    monkeypatch: pytest.MonkeyPatch,
    in_docker: bool,
) -> None:
    """The handshake forwards the probed container flag verbatim.

    Patching ``_IN_DOCKER`` pins the wiring — comparing against the live
    constant would pass even on a hardcoded ``False`` in a non-container CI.
    """
    monkeypatch.setattr(ws_module, "_IN_DOCKER", in_docker)
    client = await aiohttp_client(_bare_app())
    async with client.ws_connect("/ws") as ws:
        info = loads((await ws.receive(timeout=2.0)).data)
    assert info["ha_addon"] is False
    assert info["in_docker"] is in_docker
    assert "desktop_version" in info


async def test_server_info_carries_advertised_friendly_name(
    aiohttp_client: AiohttpClient,
) -> None:
    """The handshake forwards the advertised display name."""
    client = await aiohttp_client(_bare_app(friendly_name="buildbox"))
    async with client.ws_connect("/ws") as ws:
        info = loads((await ws.receive(timeout=2.0)).data)
    assert info["friendly_name"] == "buildbox"


async def test_server_info_withholds_friendly_name_before_auth(
    aiohttp_client: AiohttpClient,
) -> None:
    """A connection still facing the in-band auth handshake gets no host name."""
    client = await aiohttp_client(
        _bare_app(trusted=False, using_password=True, friendly_name="buildbox")
    )
    async with client.ws_connect("/ws") as ws:
        info = loads((await ws.receive(timeout=2.0)).data)
    assert info["requires_auth"] is True
    assert info["friendly_name"] == ""
