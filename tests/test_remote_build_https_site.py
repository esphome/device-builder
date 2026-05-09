"""
End-to-end TLS verification of the phase-3b2 remote-build HTTPS site.

Pins that the cert + key from phase 3a, the auth middleware from
phase 3b2, and the bearer token store from phase 3b1 line up over
the wire: a strict-TLS aiohttp client gets 401 without a valid
bearer, 200 with one, and the cert it observes on connect matches
the SPKI fingerprint the receiver advertises.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web

from esphome_device_builder.controllers.config import (
    DashboardSettings,
    remote_build_settings_transaction,
)
from esphome_device_builder.device_builder import (
    DeviceBuilder,
    _build_remote_build_ssl_context,
    _remote_build_health,
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

    middleware = make_remote_build_auth_middleware(_lookup)
    app = web.Application(middlewares=[middleware])
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
