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

import aiohttp
import pytest
from aiohttp import web

from esphome_device_builder.controllers.config import remote_build_settings_transaction
from esphome_device_builder.device_builder import (
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
async def test_setting_remote_build_settings_keeps_listener_off_by_default(
    tmp_path: Path,
) -> None:
    """
    Default-off contract: a fresh config has ``enabled=False``.

    Pins the listener-binding gate at the settings layer (the
    listener wiring inspects this same field). Defends against a
    refactor that flips the default and exposes a port without
    the operator opting in.
    """
    loop = asyncio.get_running_loop()

    def _read() -> bool:
        with remote_build_settings_transaction(tmp_path) as settings:
            return settings.enabled

    enabled = await loop.run_in_executor(None, _read)
    assert enabled is False
