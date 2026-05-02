"""Tests for the response-compression middleware.

The middleware skips compression for:
  * Clients that don't advertise ``gzip``/``deflate``.
  * Pre-encoded responses (already-gzipped sidecars).
  * Range requests (partial-content + on-the-fly compression
    aren't compatible in aiohttp).
  * Non-compressible content types (binaries, opaque images).
  * Tiny responses (< 1 KiB) where framing dominates.

It's also unwired entirely on the trusted Ingress site —
the HA supervisor proxy compresses upstream, so doing it
here would re-encode an already-encoded body.
"""

from __future__ import annotations

from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.helpers.json import (
    _COMPRESSION_MIN_BYTES,
    compression_middleware,
)


def _app(handler) -> web.Application:
    """One-handler app with just the compression middleware attached."""
    app = web.Application(middlewares=[compression_middleware])
    app.router.add_get("/", handler)
    return app


# ---------------------------------------------------------------------------
# Compression triggered
# ---------------------------------------------------------------------------


async def test_compresses_text_response_for_gzip_client(
    aiohttp_client: AiohttpClient,
) -> None:
    """A gzip-accepting client gets the body encoded.

    Body needs to be > the 1 KiB floor or the middleware would
    skip on size grounds.
    """
    body = "x" * (_COMPRESSION_MIN_BYTES + 64)

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="text/html")

    client = await aiohttp_client(_app(handler))
    resp = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.status == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    # aiohttp's client transparently decompresses; the round-trip
    # body matches the original.
    assert await resp.text() == body


async def test_compresses_javascript_response(
    aiohttp_client: AiohttpClient,
) -> None:
    """JS bundle (the dashboard's biggest payload) is in the allowlist."""
    body = "x" * (_COMPRESSION_MIN_BYTES + 64)

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="application/javascript")

    client = await aiohttp_client(_app(handler))
    resp = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.headers["Content-Encoding"] == "gzip"


async def test_compresses_svg_response(
    aiohttp_client: AiohttpClient,
) -> None:
    """``image/svg+xml`` is XML in disguise — compresses well, allowlist."""
    body = "<svg>" + ("x" * (_COMPRESSION_MIN_BYTES + 64)) + "</svg>"

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="image/svg+xml")

    client = await aiohttp_client(_app(handler))
    resp = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.headers["Content-Encoding"] == "gzip"


async def test_compresses_json_response(
    aiohttp_client: AiohttpClient,
) -> None:
    """WS / REST JSON payloads — common path, must compress."""
    body = '{"a":' + ('"x"' * 400) + "}"

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="application/json")

    client = await aiohttp_client(_app(handler))
    resp = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.headers["Content-Encoding"] == "gzip"


# ---------------------------------------------------------------------------
# Compression skipped
# ---------------------------------------------------------------------------


async def test_skips_when_client_does_not_accept_gzip(
    aiohttp_client: AiohttpClient,
) -> None:
    """No ``gzip`` / ``deflate`` in Accept-Encoding → leave the body alone.

    Curl-without-flags and a few embedded-device HTTP clients
    fall here. Compressing without an encoding negotiation would
    produce gibberish on the wire.
    """
    body = "x" * (_COMPRESSION_MIN_BYTES + 64)

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="text/html")

    client = await aiohttp_client(_app(handler))
    # ``Accept-Encoding: identity`` explicitly opts out.
    resp = await client.get("/", headers={"Accept-Encoding": "identity"})
    # aiohttp's TestClient adds ``Accept-Encoding: gzip, deflate`` by
    # default; setting identity above replaces it.
    assert resp.headers.get("Content-Encoding") in (None, "identity")


async def test_skips_already_encoded_response(
    aiohttp_client: AiohttpClient,
) -> None:
    """Pre-encoded responses pass through untouched.

    e.g. ``.gz`` sidecars served by ``FileResponse`` set
    ``Content-Encoding`` themselves; double-encoding would break
    decoders.
    """
    body = b"\x1f\x8b\x08\x00" + b"x" * (_COMPRESSION_MIN_BYTES + 64)

    async def handler(_: web.Request) -> web.Response:
        resp = web.Response(body=body, content_type="application/javascript")
        resp.headers["Content-Encoding"] = "gzip"
        return resp

    client = await aiohttp_client(_app(handler))
    resp = await client.get("/", headers={"Accept-Encoding": "gzip"}, auto_decompress=False)
    assert resp.headers["Content-Encoding"] == "gzip"
    # Body unchanged — middleware passed through without re-encoding.
    assert await resp.read() == body


async def test_skips_range_request(aiohttp_client: AiohttpClient) -> None:
    """Range requests pass through uncompressed.

    Partial-content semantics aren't compatible with on-the-fly
    compression in aiohttp — skip rather than fail.
    """
    body = "x" * (_COMPRESSION_MIN_BYTES + 64)

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="text/html")

    client = await aiohttp_client(_app(handler))
    resp = await client.get(
        "/",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=0-127"},
    )
    assert resp.headers.get("Content-Encoding") in (None, "identity")


async def test_skips_non_compressible_content_type(
    aiohttp_client: AiohttpClient,
) -> None:
    """Non-compressible content types are skipped.

    Already-compressed binaries (firmware bins, opaque images)
    rarely shrink further when re-compressed, so the encoding
    just burns CPU.
    """
    body = b"\x00\xff" * (_COMPRESSION_MIN_BYTES // 2 + 64)

    async def handler(_: web.Request) -> web.Response:
        return web.Response(body=body, content_type="application/octet-stream")

    client = await aiohttp_client(_app(handler))
    resp = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("Content-Encoding") in (None, "identity")


async def test_skips_tiny_response(aiohttp_client: AiohttpClient) -> None:
    """Tiny responses skip compression.

    Bodies below ``_COMPRESSION_MIN_BYTES`` (1 KiB) are
    dominated by response-line + header bytes — encoding
    burns CPU for a worst-case wire-size increase.
    """
    body = "tiny"

    async def handler(_: web.Request) -> web.Response:
        return web.Response(text=body, content_type="text/html")

    client = await aiohttp_client(_app(handler))
    resp = await client.get("/", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("Content-Encoding") in (None, "identity")


# ---------------------------------------------------------------------------
# Wiring on the trusted Ingress site
# ---------------------------------------------------------------------------


def _build_app(*, trusted: bool) -> web.Application:
    """Spin up an app via ``DeviceBuilder.create_app(trusted=...)``.

    Mirrors what production does: the public site enables
    compression + auth; the trusted ingress site skips both
    because the supervisor proxy compresses upstream and
    authenticates upstream of us.
    """
    from unittest.mock import MagicMock

    from esphome_device_builder.device_builder import DeviceBuilder

    db = MagicMock(spec=DeviceBuilder)
    db.settings = MagicMock()
    db.settings.dev_mode = False
    db.settings.create_ingress_site = False
    return DeviceBuilder.create_app(db, trusted=trusted, with_lifecycle=False)


async def test_public_site_includes_compression_middleware() -> None:
    """The public site has compression middleware attached."""
    app = _build_app(trusted=False)
    assert compression_middleware in app.middlewares


async def test_trusted_ingress_site_skips_compression_middleware() -> None:
    """Ingress site does NOT compress — supervisor proxy handles it.

    Re-encoding an already-encoded body would burn CPU twice for
    the same wire bytes (and the supervisor compresses with its
    own settings, so we'd be fighting it).
    """
    app = _build_app(trusted=True)
    assert compression_middleware not in app.middlewares
