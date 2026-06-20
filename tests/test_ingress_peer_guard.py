"""Coverage for ``helpers.auth.ingress_peer_guard``.

The trusted HA Ingress site bypasses auth, so the TCP peer is the only gate:
only the supervisor (172.30.32.2) and loopback (HA core's host-network
integration + the host) may reach it; a bridge add-on or LAN client gets 403.
Mirrors the legacy add-on nginx ``allow 127.0.0.1; allow 172.30.32.2; deny all``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.auth import auth_middleware, ingress_peer_guard


class _FakeTransport:
    def __init__(self, peername: tuple[str, int] | None) -> None:
        self._peername = peername

    def get_extra_info(self, name: str) -> object | None:
        return self._peername if name == "peername" else None


class _FakeRequest:
    def __init__(self, peername: tuple[str, int] | None) -> None:
        self.transport = _FakeTransport(peername)


async def _handler(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "172.30.32.2"])
async def test_allows_loopback_and_supervisor(ip: str) -> None:
    """Loopback (integration/host) and the supervisor (ingress proxy) pass through."""
    request = _FakeRequest((ip, 54321))

    resp = await ingress_peer_guard(request, _handler)  # type: ignore[arg-type]

    assert resp.status == 200


@pytest.mark.parametrize("ip", ["192.168.1.50", "172.30.33.7", "10.0.0.1"])
async def test_rejects_lan_and_bridge_peers(ip: str) -> None:
    """A LAN client or another hassio-bridge add-on is forbidden."""
    request = _FakeRequest((ip, 54321))

    resp = await ingress_peer_guard(request, _handler)  # type: ignore[arg-type]

    assert resp.status == 403


async def test_rejects_when_peer_unknown() -> None:
    """No peername (no transport info) is denied rather than allowed."""
    request = _FakeRequest(None)

    resp = await ingress_peer_guard(request, _handler)  # type: ignore[arg-type]

    assert resp.status == 403


def test_create_app_wires_peer_guard_on_trusted_site_only(tmp_path: Path) -> None:
    """``create_app(trusted=True)`` gets the peer guard; the public site gets auth."""
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()
    db = DeviceBuilder(settings)

    trusted = db.create_app(trusted=True, with_lifecycle=False)
    public = db.create_app(trusted=False, with_lifecycle=False)

    assert ingress_peer_guard in trusted.middlewares
    assert auth_middleware not in trusted.middlewares
    assert ingress_peer_guard not in public.middlewares
    assert auth_middleware in public.middlewares


def test_create_app_front_door_open_drops_peer_guard_but_keeps_origin_gate(
    tmp_path: Path,
) -> None:
    """The front-door-open public app is LAN-reachable but keeps the origin/CSRF gate.

    ``peer_guard=False`` removes the loopback/supervisor restriction the trusted
    ingress site relies on, so a non-loopback LAN peer (the VS Code plugin) reaches
    it. ``trusted=False`` leaves ``trusted_site`` off so the WS origin/Host gate
    still rejects a plain cross-origin browser drive-by; ``auth_middleware`` is
    present but a runtime no-op because the add-on configures no password (it
    short-circuits on ``not using_password``), so legit same-origin and
    Origin-less clients stay unauthenticated.
    """
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()
    db = DeviceBuilder(settings)

    front_door = db.create_app(trusted=False, peer_guard=False, with_lifecycle=False)

    assert ingress_peer_guard not in front_door.middlewares
    assert auth_middleware in front_door.middlewares
    assert front_door["trusted_site"] is False
