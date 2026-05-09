"""
End-to-end TLS verification of the phase-3b2 remote-build HTTPS site.

Covers the auth + listener wiring for the receiver site:

* A strict-TLS aiohttp client gets 401 without a valid bearer
  and 200 with one (real handshake against the cert + key from
  phase 3a, real auth middleware from phase 3b2 against the
  token store from phase 3b1).
* The lifecycle hook ``_maybe_start_remote_build_site``
  default-skips when ``enabled=False``, binds when
  ``enabled=True``, fails-soft on bind error, advertises the
  OS-assigned port for ephemeral binds, and warns on
  HA-addon mode.
* The strip-Server-header middleware removes aiohttp's default
  banner from on-the-wire responses.

Pin-vs-observed-cert verification is the pairing flow's job
(phase 4) and isn't exercised here.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from esphome_device_builder.controllers.config import (
    DashboardSettings,
    remote_build_settings_transaction,
)
from esphome_device_builder.device_builder import (
    DeviceBuilder,
    _build_remote_build_ssl_context,
    _remote_build_health,
    _strip_server_header_middleware,
)
from esphome_device_builder.helpers.dashboard_identity import (
    _CERT_FILENAME,
    get_or_create_identity,
)
from esphome_device_builder.helpers.remote_build_auth import (
    make_remote_build_auth_middleware,
)
from esphome_device_builder.models import StoredToken


async def _bring_up_site(
    tmp_path: Path,
    *,
    tokens: list[StoredToken],
) -> tuple[web.AppRunner, int]:
    """
    Stand up a real HTTPS listener bound to a real ephemeral port.

    Mirrors what ``DeviceBuilder._maybe_start_remote_build_site``
    does, but inline so the tests can drive it without booting
    the whole dashboard. Returns the runner (for cleanup) and
    the bound port.
    """
    loop = asyncio.get_running_loop()
    identity = await loop.run_in_executor(None, get_or_create_identity, tmp_path)
    ssl_ctx = await loop.run_in_executor(None, _build_remote_build_ssl_context, identity)

    by_id = {t.token_id: t for t in tokens}

    def _lookup(token_id: str) -> StoredToken | None:
        return by_id.get(token_id)

    auth_middleware = make_remote_build_auth_middleware(_lookup)
    # Mirror production's middleware stack: server-header strip
    # first (so its post-handler step runs LAST on the way out),
    # auth gate inside.
    app = web.Application(middlewares=[_strip_server_header_middleware, auth_middleware])
    app.router.add_get("/remote-build/v1/health", _remote_build_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_ctx)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, port


def _build_client_ctx(tmp_path: Path) -> ssl.SSLContext:
    """Strict client: trust only our cert, validate hostname (SAN=localhost)."""
    return ssl.create_default_context(cafile=str(tmp_path / _CERT_FILENAME))


@pytest.mark.asyncio
async def test_health_returns_401_without_bearer(tmp_path: Path) -> None:
    """No ``Authorization`` header → 401 from the auth middleware."""
    runner, port = await _bring_up_site(tmp_path, tokens=[])
    try:
        loop = asyncio.get_running_loop()
        client_ctx = await loop.run_in_executor(None, _build_client_ctx, tmp_path)
        connector = aiohttp.TCPConnector(ssl=client_ctx)
        async with (
            aiohttp.ClientSession(connector=connector) as session,
            session.get(
                f"https://localhost:{port}/remote-build/v1/health",
                server_hostname="localhost",
            ) as resp,
        ):
            assert resp.status == 401
            assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer ")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_returns_200_with_valid_bearer(tmp_path: Path) -> None:
    """A valid bearer reaches the handler and gets a 200 + JSON ack."""
    secret = "the-canary-secret"
    token = StoredToken(
        token_id="abc123",
        label="Green",
        secret_sha256=hashlib.sha256(secret.encode("ascii")).hexdigest(),
        created_at=1.0,
    )
    runner, port = await _bring_up_site(tmp_path, tokens=[token])
    try:
        loop = asyncio.get_running_loop()
        client_ctx = await loop.run_in_executor(None, _build_client_ctx, tmp_path)
        connector = aiohttp.TCPConnector(ssl=client_ctx)
        async with (
            aiohttp.ClientSession(connector=connector) as session,
            session.get(
                f"https://localhost:{port}/remote-build/v1/health",
                server_hostname="localhost",
                headers={"Authorization": f"Bearer abc123.{secret}"},
            ) as resp,
        ):
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True}
            # On-the-wire check: aiohttp injects a ``Server``
            # banner at the connection-write layer when the
            # response doesn't carry one. The strip-Server
            # middleware sets it to empty string so aiohttp's
            # default banner is overridden. Empty value (not
            # absent) is the expected wire shape.
            assert resp.headers.get("Server", "") == ""
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_skips_when_disabled(tmp_path: Path) -> None:
    """
    Default-off: ``_maybe_start_remote_build_site`` early-returns when ``enabled=False``.

    Pins the gate at the lifecycle hook, not just at the
    settings layer — a refactor that bound the listener
    unconditionally (or read the wrong field) would fail here
    even if ``RemoteBuildSettings.enabled`` still defaulted to
    ``False``.
    """
    settings = DashboardSettings(config_dir=tmp_path)
    db = DeviceBuilder(settings)
    db.loop = asyncio.get_running_loop()
    db.remote_build = MagicMock()

    await db._maybe_start_remote_build_site()
    assert db._remote_build_runner is None


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_binds_when_enabled(tmp_path: Path) -> None:
    """
    Flipping ``enabled=True`` makes the lifecycle hook bind the listener.

    Round-trip: write ``enabled=True`` to the settings sidecar,
    drive ``_maybe_start_remote_build_site`` through the same
    code path the dashboard's startup uses, assert a runner
    landed.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    # Pin the port to ``0`` so the OS picks a free one and the
    # test doesn't collide with a real receiver if 6055 is in use.
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build = MagicMock()
    db.remote_build.lookup_token = MagicMock(return_value=None)

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_fails_soft_on_bind_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed bind logs the error and leaves the dashboard running.

    Drive ``_maybe_start_remote_build_site`` through the enabled
    path with a port that fails to bind (port 1, can't bind as
    non-root). The hook MUST NOT raise; the runner must end up
    cleaned up; the dashboard's main flow continues unaffected.
    Pins the fail-soft contract so a misconfiguration in
    Settings (typo'd port, port already in use, cert load
    failure) doesn't take down the whole dashboard.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    # Force the bind to fail by stubbing TCPSite.start to raise.
    real_start = web.TCPSite.start

    async def _failing_start(self: web.TCPSite) -> None:
        raise OSError("address in use (test stub)")

    monkeypatch.setattr(web.TCPSite, "start", _failing_start)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build = MagicMock()
    db.remote_build.lookup_token = MagicMock(return_value=None)

    # Must not raise — the dashboard keeps running on bind failure.
    await db._maybe_start_remote_build_site()
    assert db._remote_build_runner is None

    # Sanity: with the stub removed, a fresh call would succeed.
    monkeypatch.setattr(web.TCPSite, "start", real_start)


@pytest.mark.asyncio
async def test_strip_server_header_middleware_overrides_to_empty(tmp_path: Path) -> None:
    """
    The Server header is overridden to empty string.

    Setting to empty (not deleting) is what overrides aiohttp's
    connection-level default banner; the live HTTPS test in this
    file pins the on-the-wire shape end-to-end. This unit test
    just sanity-checks the middleware's response-level behaviour.
    """

    async def _handler(_: web.Request) -> web.StreamResponse:
        return web.Response(status=200, headers={"Server": "Python/3.14 aiohttp/3.13"})

    request = make_mocked_request("GET", "/remote-build/v1/health", client_max_size=0)
    response = await _strip_server_header_middleware(request, _handler)
    assert response.headers["Server"] == ""


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_updates_advertiser_on_success(
    tmp_path: Path,
) -> None:
    """
    Successful bind pushes ``pin_sha256`` + ``remote_build_port`` into the advertiser.

    Pins the post-bind advertiser-update wiring so a refactor that
    accidentally drops the setter calls (or moves them before the
    bind) surfaces here.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build = MagicMock()
    db.remote_build.lookup_token = MagicMock(return_value=None)

    fake_advertiser = MagicMock()
    fake_advertiser.set_pin_sha256 = MagicMock()
    fake_advertiser.set_remote_build_port = MagicMock()
    fake_advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = fake_advertiser

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
        # SPKI pin and listener port both made it to the advertiser.
        assert fake_advertiser.set_pin_sha256.called
        assert fake_advertiser.set_remote_build_port.called
        # ``refresh`` was awaited so the TXT change actually
        # leaves the local cache.
        assert fake_advertiser.refresh.called
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_advertises_actual_port_for_ephemeral(
    tmp_path: Path,
) -> None:
    """
    ``remote_build_port=0`` advertises the OS-assigned port, not literal 0.

    When the operator binds with ``--remote-build-port 0`` (or a
    test pins it to 0 to avoid collisions), the OS picks an
    ephemeral port. Advertising or logging ``0`` would point
    peers at an unreachable port and the operator couldn't
    answer "what port am I on?". Resolve the actual bound port
    from the socket and pass that to the advertiser.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0  # ask the OS for an ephemeral port
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build = MagicMock()
    db.remote_build.lookup_token = MagicMock(return_value=None)

    fake_advertiser = MagicMock()
    fake_advertiser.set_pin_sha256 = MagicMock()
    fake_advertiser.set_remote_build_port = MagicMock()
    fake_advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = fake_advertiser

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
        # The advertiser receives the OS-assigned port, never 0.
        assert fake_advertiser.set_remote_build_port.called
        advertised = fake_advertiser.set_remote_build_port.call_args.args[0]
        assert advertised != 0
        assert 1024 <= advertised <= 65535
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_warns_on_ha_addon(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HA-addon mode logs a warning when the listener binds."""
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    settings.on_ha_addon = True  # the branch under test
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build = MagicMock()
    db.remote_build.lookup_token = MagicMock(return_value=None)

    with caplog.at_level("WARNING", logger="esphome_device_builder.device_builder"):
        try:
            await db._maybe_start_remote_build_site()
            assert db._remote_build_runner is not None
        finally:
            if db._remote_build_runner is not None:
                await db._remote_build_runner.cleanup()
    warnings = [r for r in caplog.records if "HA addon" in r.getMessage()]
    assert warnings, "expected an HA-addon warning"
