"""End-to-end TLS verification of the generated identity.

Pins that the cert + key shipped by ``get_or_create_identity``
work through Python's ``ssl`` module and aiohttp's TLS stack
under standard X.509 validation, not just under the
SPKI-pinning model peers will use. If a future change to the
cert (algorithm swap, extension flip, validity window) breaks
strict validation, these tests fail loudly so the regression
surfaces here rather than the first time a non-pinning client
(future browser-based admin flow, curl, or a stricter TLS stack)
tries to connect.
"""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from esphome_device_builder.helpers.dashboard_identity import (
    _CERT_FILENAME,
    _KEY_FILENAME,
    get_or_create_identity,
)


def _build_server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx


def _build_client_ssl_context(cert_path: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cert_path))


def test_cert_and_key_load_into_python_ssl(tmp_path: Path) -> None:
    """``ssl.SSLContext.load_cert_chain`` accepts the Ed25519 cert + key."""
    get_or_create_identity(tmp_path)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(
        certfile=str(tmp_path / _CERT_FILENAME),
        keyfile=str(tmp_path / _KEY_FILENAME),
    )


@pytest.mark.asyncio
async def test_aiohttp_https_handshake_passes_strict_x509(tmp_path: Path) -> None:
    """
    A strict X.509 client (full hostname + chain validation) handshakes cleanly.

    Trust our self-signed cert as the only CA, require hostname
    match against the SAN (``localhost``), and verify the chain.
    Failure here would mean Python's ssl rejected something about
    the cert (Ed25519 signature, critical SERVER_AUTH EKU, the
    SAN extension, or the validity window) before the request
    even left the client.
    """
    # The identity helper does sync file I/O (existence check, PEM reads,
    # ed25519 generate, atomic_write); call it through the executor so
    # blockbuster doesn't flag it as a blocking call from the loop.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, get_or_create_identity, tmp_path)
    cert_path = tmp_path / _CERT_FILENAME
    key_path = tmp_path / _KEY_FILENAME

    server_ctx = await loop.run_in_executor(None, _build_server_ssl_context, cert_path, key_path)

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text="hello")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ctx)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    try:
        client_ctx = await loop.run_in_executor(None, _build_client_ssl_context, cert_path)
        # Defaults: check_hostname=True, verify_mode=CERT_REQUIRED.
        connector = aiohttp.TCPConnector(ssl=client_ctx)
        async with (
            aiohttp.ClientSession(connector=connector) as session,
            session.get(
                f"https://localhost:{port}/",
                server_hostname="localhost",
            ) as resp,
        ):
            assert resp.status == 200
            assert await resp.text() == "hello"
    finally:
        await runner.cleanup()
