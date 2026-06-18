"""Coverage for the public ``GET /version`` health/version endpoint.

The upstream esphome Docker image's HEALTHCHECK curls ``/version`` with no
credentials. It must return a JSON 200 even when a password is set, so the
route sits in ``auth_middleware``'s public allowlist. This drives the real
middleware + ``_handle_version`` through an aiohttp test client and pins that
a password-protected install still answers with ``{"version": ...}``.
"""

from __future__ import annotations

from aiohttp import web
from esphome.const import __version__ as esphome_version
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.device_builder import _handle_version
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
