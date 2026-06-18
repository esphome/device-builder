"""Coverage for the public ``GET /version`` health/version endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from esphome.const import __version__ as esphome_version
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.device_builder import DeviceBuilder, _handle_version
from esphome_device_builder.helpers.auth import auth_middleware


class _StubSessionStore:
    async def validate(self, token: str) -> object | None:
        return None


class _StubRateLimiter:
    def remaining_lockout(self, ip: str) -> float:
        return 0.0

    def clear(self, ip: str) -> None: ...

    def record_failure(self, ip: str) -> None: ...


class _StubAuth:
    def __init__(self) -> None:
        self.session_store = _StubSessionStore()
        self.rate_limiter = _StubRateLimiter()


class _StubSettings:
    def __init__(self, *, using_password: bool) -> None:
        self.using_password = using_password

    def check_password(self, username: str, password: str) -> bool:
        return False


class _StubDeviceBuilder:
    def __init__(self, *, using_password: bool) -> None:
        self.settings = _StubSettings(using_password=using_password)
        self.auth = _StubAuth()


def _make_app(db: _StubDeviceBuilder) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["device_builder"] = db
    app.router.add_get("/version", _handle_version)
    return app


async def test_version_endpoint_answers_without_auth(aiohttp_client: AiohttpClient) -> None:
    """``/version`` returns the esphome version JSON even with a password set."""
    client = await aiohttp_client(_make_app(_StubDeviceBuilder(using_password=True)))

    resp = await client.get("/version")

    assert resp.status == 200
    assert await resp.json() == {"version": esphome_version}


async def test_version_route_wins_over_spa_catch_all(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Real ``create_app``: ``/version`` serves JSON, not the SPA shell."""
    pytest.importorskip("esphome_device_builder_frontend")
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()
    db = DeviceBuilder(settings)
    client = await aiohttp_client(db.create_app(with_lifecycle=False))

    version_resp = await client.get("/version")
    assert version_resp.status == 200
    assert await version_resp.json() == {"version": esphome_version}

    # The SPA catch-all is live (a deep link returns the shell), so the
    # JSON above proves /version won the FIFO match rather than there
    # being no catch-all to lose to.
    spa_resp = await client.get("/some/deep/link")
    assert spa_resp.status == 200
    assert "<!doctype html>" in (await spa_resp.text()).lower()
